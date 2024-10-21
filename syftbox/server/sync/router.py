import base64
import sqlite3
import tempfile
from pathlib import Path

import py_fast_rsync
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from syftbox.server.sync.db import (
    delete_file_metadata,
    get_all_metadata,
    get_db,
    move_with_transaction,
)
from syftbox.server.sync.hash import hash_file

from .models import (
    ApplyDiffRequest,
    ApplyDiffResponse,
    DiffRequest,
    DiffResponse,
    FileMetadata,
    FileMetadataRequest,
    FileRequest,
)


def get_db_connection(request: Request):
    conn = get_db(request.state.server_settings.file_db_path)
    yield conn
    conn.close()


def get_file_metadata(
    req: FileMetadataRequest,
    conn=Depends(get_db_connection),
) -> list[FileMetadata]:
    # TODO check permissions

    return get_all_metadata(conn, path_like=req.path_like)


router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/get_diff", response_model=DiffResponse)
def get_diff(
    req: DiffRequest,
    conn: sqlite3.Connection = Depends(get_db_connection),
) -> DiffResponse:
    metadata_list = get_all_metadata(conn, path_like=f"%{req.path}%")
    if len(metadata_list) == 0:
        raise HTTPException(status_code=404, detail="path not found")
    elif len(metadata_list) > 1:
        raise HTTPException(status_code=400, detail="too many files to get diff")

    metadata = metadata_list[0]

    with open(metadata.path, "rb") as f:
        data = f.read()

    diff = py_fast_rsync.diff(req.signature_bytes, data)
    diff_bytes = base64.b85encode(diff).decode("utf-8")
    return DiffResponse(
        path=metadata.path.as_posix(),
        diff=diff_bytes,
        hash=metadata.hash,
    )


@router.post("/get_metadata", response_model=list[FileMetadata])
def get_metadata(
    metadata: list[FileMetadata] = Depends(get_file_metadata),
) -> list[FileMetadata]:
    return metadata


@router.post("/apply_diff", response_model=ApplyDiffResponse)
def apply_diffs(
    req: ApplyDiffRequest,
    conn: sqlite3.Connection = Depends(get_db_connection),
) -> ApplyDiffResponse:
    metadata_list = get_all_metadata(conn, path_like=f"%{req.path}%")
    if len(metadata_list) == 0:
        raise HTTPException(status_code=404, detail="path not found")
    elif len(metadata_list) > 1:
        raise HTTPException(
            status_code=400, detail="found too many files to apply diff"
        )

    metadata = metadata_list[0]

    with open(metadata.path, "rb") as f:
        data = f.read()
    result = py_fast_rsync.apply(data, req.diff_bytes)

    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        temp_file.write(result)
        temp_path = temp_file.name

    new_metadata = hash_file(temp_path)

    if new_metadata.hash != req.expected_hash:
        raise HTTPException(status_code=400, detail="expected_hash mismatch")

    # move temp path to real path and update db
    move_with_transaction(
        conn,
        metadata=new_metadata,
        origin_path=metadata.path,
    )

    return ApplyDiffResponse(
        path=req.path, current_hash=new_metadata.sha256, previous_hash=metadata.hash
    )


@router.post("/delete", response_class=JSONResponse)
def delete_file(
    req: FileRequest,
    conn: sqlite3.Connection = Depends(get_db_connection),
) -> JSONResponse:
    metadata_list = get_all_metadata(conn, path_like=f"%{req.path}%")
    if len(metadata_list) == 0:
        raise HTTPException(status_code=404, detail="path not found")
    elif len(metadata_list) > 1:
        raise HTTPException(status_code=400, detail="too many files to delete")

    metadata = metadata_list[0]

    delete_file_metadata(conn, metadata.path.as_posix())
    Path(metadata.path).unlink(missing_ok=True)
    return JSONResponse(content={"status": "success"})


@router.post("/create", response_class=JSONResponse)
def create_file(
    file: UploadFile,
    conn: sqlite3.Connection = Depends(get_db_connection),
) -> JSONResponse:
    # there is probably a better way to do this
    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        temp_file.write(file.read())
        temp_path = temp_file.name

    metadata = hash_file(temp_path)
    target_path = ...
    move_with_transaction(conn, metadata=metadata, origin_path=target_path)
    return JSONResponse(content={"status": "success"})
