import hashlib
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional

import py_fast_rsync
from loguru import logger
from pydantic import BaseModel

from syftbox.client.exceptions import SyftServerError
from syftbox.client.plugins.sync.constants import MAX_FILE_SIZE_MB
from syftbox.client.plugins.sync.datasite_state import DatasiteState
from syftbox.client.plugins.sync.exceptions import FatalSyncError, SyncEnvironmentError
from syftbox.client.plugins.sync.local_state import LocalState
from syftbox.client.plugins.sync.queue import SyncQueue, SyncQueueItem
from syftbox.client.plugins.sync.sync_client import SyncClient
from syftbox.client.plugins.sync.types import SyncActionType, SyncDecisionType, SyncSide, SyncStatus
from syftbox.lib.ignore import filter_ignored_paths
from syftbox.lib.lib import SyftPermission
from syftbox.server.sync.hash import hash_file
from syftbox.server.sync.models import FileMetadata


def update_local(
    sync_client: SyncClient,
    local_syncstate: FileMetadata,
) -> None:
    diff = sync_client.get_diff(local_syncstate.path, local_syncstate.signature)

    abs_path = sync_client.workspace.datasites / local_syncstate.path
    local_data = abs_path.read_bytes()

    new_data = py_fast_rsync.apply(local_data, diff.diff_bytes)
    new_hash = hashlib.sha256(new_data).hexdigest()

    if new_hash != diff.hash:
        # TODO handle
        raise ValueError("hash mismatch")

    # TODO implement safe write with tempfile + rename
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(new_data)


def update_remote(
    sync_client: SyncClient,
    local_syncstate: FileMetadata,
    remote_syncstate: FileMetadata,
) -> None:
    abs_path = sync_client.workspace.datasites / local_syncstate.path
    local_data = abs_path.read_bytes()
    diff = py_fast_rsync.diff(remote_syncstate.signature_bytes, local_data)
    sync_client.apply_diff(
        relative_path=local_syncstate.path,
        diff=diff,
        expected_hash=remote_syncstate.hash,
    )


def delete_local(sync_client: SyncClient, remote_syncstate: FileMetadata) -> None:
    abs_path = sync_client.workspace.datasites / remote_syncstate.path
    abs_path.unlink()


def delete_remote(sync_client: SyncClient, remote_syncstate: FileMetadata) -> None:
    sync_client.delete(remote_syncstate.path)


def create_local(sync_client: SyncClient, remote_syncstate: FileMetadata):
    abs_path = sync_client.workspace.datasites / remote_syncstate.path
    content_bytes = sync_client.download(remote_syncstate.path)
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(content_bytes)


def create_local_batch(sync_client: SyncClient, remote_syncstates: list[Path]) -> list[str]:
    paths = [str(path) for path in remote_syncstates]
    try:
        content_bytes = sync_client.download_bulk(paths)
    except SyftServerError as e:
        logger.error(e)
        return []
    zip_file = zipfile.ZipFile(BytesIO(content_bytes))
    zip_file.extractall(sync_client.workspace.datasites)
    return zip_file.namelist()


def create_remote(sync_client: SyncClient, local_syncstate: FileMetadata):
    abs_path = sync_client.workspace.datasites / local_syncstate.path
    data = abs_path.read_bytes()
    sync_client.create(local_syncstate.path, data)


class SyncDecision(BaseModel):
    operation: SyncDecisionType
    side_to_update: SyncSide
    local_syncstate: Optional[FileMetadata]
    remote_syncstate: Optional[FileMetadata]

    is_executed: bool = False
    message: Optional[str] = None

    def execute(self, client: SyncClient):
        try:
            if self.operation == SyncDecisionType.NOOP:
                pass
            elif self.action_type == SyncActionType.CREATE_REMOTE:
                create_remote(client, self.local_syncstate)
            elif self.action_type == SyncActionType.CREATE_LOCAL:
                create_local(client, self.remote_syncstate)
            elif self.action_type == SyncActionType.DELETE_REMOTE:
                delete_remote(client, self.remote_syncstate)
            elif self.action_type == SyncActionType.DELETE_LOCAL:
                delete_local(client, self.local_syncstate)
            elif self.action_type == SyncActionType.MODIFY_REMOTE:
                update_remote(client, self.local_syncstate, self.remote_syncstate)
            elif self.action_type == SyncActionType.MODIFY_LOCAL:
                update_local(client, self.local_syncstate)

            self.is_executed = True

        except FatalSyncError:
            raise
        # TODO add more specific exception handling for connection errors, rejected files, etc.
        except Exception as e:
            self.is_executed = False
            self.message = f"{type(e).__name__}: {str(e)}"
            logger.error(f"Failed to sync file {self.path}, it will be retried in the next sync. Reason: {e}")

    @property
    def path(self) -> Path:
        if self.local_syncstate:
            return self.local_syncstate.path
        elif self.remote_syncstate:
            return self.remote_syncstate.path

        raise ValueError("No path found in SyncDecision")

    def is_noop(self):
        return self.operation == SyncDecisionType.NOOP

    @property
    def action_type(self):
        if self.operation == SyncDecisionType.NOOP:
            return SyncActionType.NOOP
        if self.operation == SyncDecisionType.CREATE and self.side_to_update == SyncSide.LOCAL:
            return SyncActionType.CREATE_LOCAL
        elif self.operation == SyncDecisionType.CREATE and self.side_to_update == SyncSide.REMOTE:
            return SyncActionType.CREATE_REMOTE
        elif self.operation == SyncDecisionType.DELETE and self.side_to_update == SyncSide.LOCAL:
            return SyncActionType.DELETE_LOCAL
        elif self.operation == SyncDecisionType.DELETE and self.side_to_update == SyncSide.REMOTE:
            return SyncActionType.DELETE_REMOTE
        elif self.operation == SyncDecisionType.MODIFY and self.side_to_update == SyncSide.LOCAL:
            return SyncActionType.MODIFY_LOCAL
        elif self.operation == SyncDecisionType.MODIFY and self.side_to_update == SyncSide.REMOTE:
            return SyncActionType.MODIFY_REMOTE

    @classmethod
    def noop(
        cls,
        local_syncstate: FileMetadata,
        remote_syncstate: FileMetadata,
    ):
        return cls(
            operation=SyncDecisionType.NOOP,
            side_to_update=SyncSide.LOCAL,
            local_syncstate=local_syncstate,
            remote_syncstate=remote_syncstate,
        )

    @classmethod
    def from_modified_states(
        cls,
        local_syncstate: Optional[FileMetadata],
        remote_syncstate: Optional[FileMetadata],
        side_to_update: SyncSide,
    ):
        """Asssumes at least on of the states is modified"""

        delete = (
            side_to_update == SyncSide.REMOTE
            and local_syncstate is None
            or side_to_update == SyncSide.LOCAL
            and remote_syncstate is None
        )

        create = (
            side_to_update == SyncSide.REMOTE
            and remote_syncstate is None
            or side_to_update == SyncSide.LOCAL
            and local_syncstate is None
        )

        if delete:
            operation = SyncDecisionType.DELETE
        elif create:
            operation = SyncDecisionType.CREATE
        else:
            operation = SyncDecisionType.MODIFY

        return cls(
            operation=operation,
            side_to_update=side_to_update,
            local_syncstate=local_syncstate,
            remote_syncstate=remote_syncstate,
        )

    def _is_invalid_remote_permission_change(self, local_abs_path: Path) -> bool:
        # we want to make sure that
        # 1) We never upload invalid syftperm files
        # 2) We allow for modifications/deletions of syftperm files, even if the local version
        # is corrupted

        if self.side_to_update != SyncSide.REMOTE:
            # Decision does not update remote, no need to check for invalid perm file
            return False

        remote_op = self.operation
        is_invalid_permission_change = (
            remote_op in [SyncDecisionType.CREATE, SyncDecisionType.MODIFY]
            and SyftPermission.is_permission_file(local_abs_path)
            and not SyftPermission.is_valid(local_abs_path)
        )
        return is_invalid_permission_change

    def _is_valid_remote_decision(self, abs_path: Path) -> tuple[bool, str]:
        if self.operation in [SyncDecisionType.NOOP, SyncDecisionType.DELETE]:
            return True, ""

        # Create/modify without file data
        if self.local_syncstate is None:
            return False, f"Attempted to sync file {abs_path} to remote, but local file data is missing."

        # Create/modify invalid permission file
        if self._is_invalid_remote_permission_change(abs_path):
            return False, f"Found invalid permission {abs_path}, permission will not be synced to remote."

        # Create/modify file over max size
        max_size_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
        if self.local_syncstate.file_size > max_size_bytes:
            return False, f"File {abs_path} is larger than {MAX_FILE_SIZE_MB}MB, it will not be synced to remote."

        return True, ""

    def _is_valid_local_decision(self, abs_path: Path) -> tuple[bool, str]:
        if self.operation in [SyncDecisionType.NOOP, SyncDecisionType.DELETE]:
            return True, ""

        # Create/modify without file data
        if self.remote_syncstate is None:
            return False, f"Attempted to sync file {abs_path} to local, but remote file data is missing."

        # Create/modify file over max size
        max_size_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
        if self.remote_syncstate.file_size > max_size_bytes:
            return False, f"File {abs_path} is larger than {MAX_FILE_SIZE_MB}MB, it will not be synced to local."

        return True, ""

    def is_valid(self, abs_path: Path, show_warnings: bool = False) -> bool:
        """
        Returns True if the sync decision is valid and should be executed.
        If show_warnings is True, it will log warnings for invalid decisions.

        Args:
            abs_path (Path): Absolute path of the file to sync.
            show_warnings (bool, optional): If True, a warning will be logged for invalid decisions. Defaults to False.

        Returns:
            bool: True if the decision should be executed.
        """
        if self.side_to_update == SyncSide.REMOTE:
            is_valid, reason = self._is_valid_remote_decision(abs_path)
        elif self.side_to_update == SyncSide.LOCAL:
            is_valid, reason = self._is_valid_local_decision(abs_path)
        else:
            is_valid, reason = True, ""

        if not is_valid and show_warnings:
            logger.warning(reason)

        return is_valid


class SyncDecisionTuple(BaseModel):
    remote_decision: SyncDecision
    local_decision: SyncDecision

    @property
    def result_local_state(self) -> FileMetadata:
        if self.local_decision.operation == SyncDecisionType.NOOP:
            return self.local_decision.local_syncstate
        else:
            return self.local_decision.remote_syncstate

    @property
    def is_executed(self) -> bool:
        return self.local_decision.is_executed and self.remote_decision.is_executed

    @classmethod
    def from_states(
        cls,
        current_local_syncstate: Optional[FileMetadata],
        previous_local_syncstate: Optional[FileMetadata],
        current_remote_syncstate: Optional[FileMetadata],
    ):
        def noop() -> SyncDecision:
            return SyncDecision.noop(
                local_syncstate=current_local_syncstate,
                remote_syncstate=current_remote_syncstate,
            )

        local_modified = current_local_syncstate != previous_local_syncstate
        remote_modified = previous_local_syncstate != current_remote_syncstate
        in_sync = current_remote_syncstate == current_local_syncstate
        conflict = local_modified and remote_modified and not in_sync

        path = current_local_syncstate.path if current_local_syncstate else current_remote_syncstate.path
        logger.debug(
            f"{path} local_modified: {local_modified}, remote_modified: {remote_modified}, in_sync: {in_sync}, conflict: {conflict}"
        )

        if in_sync:
            return cls(
                remote_decision=noop(),
                local_decision=noop(),
            )
        elif conflict:
            # in case of conflict we always use the server state, because it was updated earlier
            remote_decision = noop()
            # we apply the server state locally
            local_decision = SyncDecision.from_modified_states(
                local_syncstate=current_local_syncstate,
                remote_syncstate=current_remote_syncstate,
                side_to_update=SyncSide.LOCAL,
            )
            return cls(remote_decision=remote_decision, local_decision=local_decision)
        else:
            # here we can assume only one party changed
            # assert (local_modified and not server_modified) or (server_modified and not local_modified)
            if local_modified:
                return cls(
                    local_decision=noop(),
                    remote_decision=SyncDecision.from_modified_states(
                        local_syncstate=current_local_syncstate,
                        remote_syncstate=current_remote_syncstate,
                        side_to_update=SyncSide.REMOTE,
                    ),
                )
            else:
                return cls(
                    local_decision=SyncDecision.from_modified_states(
                        local_syncstate=current_local_syncstate,
                        remote_syncstate=current_remote_syncstate,
                        side_to_update=SyncSide.LOCAL,
                    ),
                    remote_decision=noop(),
                )

    def is_noop(self) -> bool:
        return (
            self.local_decision.operation == SyncDecisionType.NOOP
            and self.remote_decision.operation == SyncDecisionType.NOOP
        )

    @property
    def info_message(self) -> Optional[str]:
        messages = []
        if self.local_decision.operation != SyncDecisionType.NOOP:
            messages.append(f"Syncing {self.local_decision.path} with decision: {self.local_decision.action_type.name}")
        if self.remote_decision.operation != SyncDecisionType.NOOP:
            messages.append(
                f"Syncing {self.remote_decision.path} with decision: {self.remote_decision.action_type.name}"
            )

        return ". ".join(messages) if messages else "Syncing {self.local_decision.path} with decision: NOOP"


class SyncConsumer:
    def __init__(self, client: SyncClient, queue: SyncQueue, local_state: LocalState):
        self.client = client
        self.queue = queue
        self.local_state = local_state

    def validate_sync_environment(self):
        if not Path(self.client.workspace.datasites).is_dir():
            raise SyncEnvironmentError("Your sync folder has been deleted by a different process.")
        if not self.local_state.path.is_file():
            raise SyncEnvironmentError("Your previous sync state has been deleted by a different process.")

    def consume_all(self):
        while not self.queue.empty():
            self.validate_sync_environment()
            item = self.queue.get(timeout=0.1)
            try:
                self.process_filechange(item)
            except FatalSyncError as e:
                # Fatal error, syncing should be interrupted
                raise e
            except Exception as e:
                logger.error(f"Failed to sync file {item.data.path}, it will be retried in the next sync. Reason: {e}")

    def download_all_missing(self, datasite_states: list[DatasiteState]):
        try:
            missing_files: list[Path] = []
            for datasite_state in datasite_states:
                for file in datasite_state.remote_state:
                    path = file.path
                    if not self.local_state.states.get(path):
                        missing_files.append(path)
            missing_files = filter_ignored_paths(self.client.workspace.datasites, missing_files)

            logger.info(f"Downloading {len(missing_files)} files in batch")
            received_files = create_local_batch(self.client, missing_files)
            for path in received_files:
                path = Path(path)
                state = self.get_current_local_syncstate(path)
                self.local_state.insert_synced_file(
                    path=path,
                    state=state,
                    action=SyncActionType.CREATE_LOCAL,
                )
        except FatalSyncError as e:
            raise e
        except Exception as e:
            logger.error(
                f"Failed to download missing files, files will be downloaded individually instead. Reason: {e}"
            )

    def get_decisions(self, item: SyncQueueItem) -> SyncDecisionTuple:
        path = item.data.path
        current_local_syncstate: FileMetadata = self.get_current_local_syncstate(path)
        previous_local_syncstate = self.get_previous_local_syncstate(path)
        # TODO, rename to remote
        current_server_state = self.get_current_server_state(path)

        return SyncDecisionTuple.from_states(current_local_syncstate, previous_local_syncstate, current_server_state)

    def process_decision(self, item: SyncQueueItem, decision: SyncDecisionTuple) -> SyncDecisionTuple:
        abs_path = item.data.local_abs_path

        # Ensure no changes are made if the sync environment is corrupted
        self.validate_sync_environment()
        if decision.local_decision.is_valid(abs_path=abs_path, show_warnings=True):
            decision.local_decision.execute(self.client)

        if decision.remote_decision.is_valid(abs_path=abs_path, show_warnings=True):
            decision.remote_decision.execute(self.client)

        return decision

    def write_to_local_state(self, item: SyncQueueItem, decisions: SyncDecisionTuple) -> None:
        if decisions.is_executed:
            local_action = decisions.local_decision.action_type
            remote_action = decisions.remote_decision.action_type
            action = local_action if local_action != SyncActionType.NOOP else remote_action
            self.local_state.insert_synced_file(
                path=item.data.path,
                state=decisions.result_local_state,
                action=action,
            )
        elif decisions.is_noop():
            return
        else:
            # Not executed and not NOOP means there was an error. Log error to local state
            decision = decisions.local_decision if not decisions.local_decision.is_noop() else decisions.remote_decision
            action = decision.action_type
            message = decision.message
            self.local_state.insert_status_info(
                path=item.data.path,
                status=SyncStatus.ERROR,
                message=message,
                action=action,
            )

    def process_filechange(self, item: SyncQueueItem) -> None:
        decisions = self.get_decisions(item)
        if not decisions.is_noop():
            logger.info(decisions.info_message)

        decisions = self.process_decision(item, decisions)
        self.write_to_local_state(item, decisions)

    def get_current_local_syncstate(self, path: Path) -> Optional[FileMetadata]:
        abs_path = self.client.workspace.datasites / path
        if not abs_path.is_file():
            return None
        return hash_file(abs_path, root_dir=self.client.workspace.datasites)

    def get_previous_local_syncstate(self, path: Path) -> Optional[FileMetadata]:
        return self.local_state.states.get(path, None)

    def get_current_server_state(self, path: Path) -> Optional[FileMetadata]:
        try:
            return self.sync_client.get_metadata(path)
        except SyftServerError:
            return None
