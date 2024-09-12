import argparse
import hashlib
import json
import os
import random
import sys
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from lib import FileChange, FileChangeKind, Jsonable, strtobin

DATA_FOLDER = "data"
SNAPSHOT_FOLDER = f"{DATA_FOLDER}/snapshot"
CHANGELOG_FOLDER = f"{DATA_FOLDER}/changelog"
USER_FILE_PATH = f"{DATA_FOLDER}/users.json"

FOLDERS = [DATA_FOLDER, SNAPSHOT_FOLDER, CHANGELOG_FOLDER]


def load_list(cls, filepath: str) -> list[Any]:
    try:
        with open(filepath) as f:
            data = f.read()
            d = json.loads(data)
            ds = []
            for di in d:
                ds.append(cls(**di))
            return ds
    except Exception as e:
        print(f"Unable to load file: {filepath}. {e}")
    return None


def save_list(obj: Any, filepath: str) -> None:
    dicts = []
    for d in obj:
        dicts.append(d.to_dict())
    with open(filepath, "w") as f:
        f.write(json.dumps(dicts))


def load_dict(cls, filepath: str) -> list[Any]:
    try:
        with open(filepath) as f:
            data = f.read()
            d = json.loads(data)
            dicts = {}
            for key, value in d.items():
                dicts[key] = cls(**value)
            return dicts
    except Exception as e:
        print(f"Unable to load file: {filepath}. {e}")
    return None


def save_dict(obj: Any, filepath: str) -> None:
    dicts = {}
    for key, value in obj.items():
        dicts[key] = value.to_dict()

    with open(filepath, "w") as f:
        f.write(json.dumps(dicts))


@dataclass
class User(Jsonable):
    email: str
    token: int  # TODO


class Users:
    def __init__(self) -> None:
        self.users = {}
        self.load()

    def load(self):
        users = load_dict(User, USER_FILE_PATH)
        if users:
            self.users = users

    def save(self):
        save_dict(self.users, USER_FILE_PATH)

    def get_user(self, email: str) -> User | None:
        if email not in self.users:
            return None
        return self.users[email]

    def create_user(self, email: str) -> int:
        if email in self.users:
            # for now just return the token
            return self.users[email].token
            # raise Exception(f"User already registered: {email}")
        token = random.randint(0, sys.maxsize)
        user = User(email=email, token=token)
        self.users[email] = user
        self.save()
        return token

    def __repr__(self) -> str:
        string = ""
        for email, user in self.users.items():
            string += f"{email}: {user}"
        return string

    # def key_for_email(self, email: str) -> int | None:
    #     user = self.get_user(email)
    #     if user:
    #         return user.public_key
    #     return None


USERS = Users()

app = FastAPI()


def create_folders(folders: list[str]) -> None:
    for folder in folders:
        if not os.path.exists(folder):
            os.makedirs(folder, exist_ok=True)


def get_file_hash(file_path: str) -> str:
    with open(file_path, "rb") as file:
        return hashlib.md5(file.read()).hexdigest()


@app.on_event("startup")
async def startup_event():
    print("> Creating Folders")
    create_folders(FOLDERS)
    print("> Loading Users")
    print(USERS)


@app.post("/register")
async def register(request: Request):
    data = await request.json()
    email = data["email"]
    token = USERS.create_user(email)
    print(f"> {email} registering: {token}")
    return JSONResponse({"status": "success", "token": token}, status_code=200)


@app.post("/write")
async def write(request: Request):
    data = await request.json()
    email = data["email"]
    change_dict = data["change"]
    change_dict["kind"] = FileChangeKind(change_dict["kind"])
    change = FileChange(**change_dict)

    change.sync_folder = os.path.abspath(SNAPSHOT_FOLDER)

    if change.kind_write:
        bin_data = strtobin(data["data"])
        result = change.write(bin_data)
    elif change.kind_delete:
        result = change.delete()
    else:
        raise Exception(f"Unknown type of change kind. {change.kind}")
    if result:
        print(f"> {email} {change.kind}: {change.internal_path}")
        return JSONResponse(
            {"status": "success", "change": change.to_dict()}, status_code=200
        )
    else:
        return JSONResponse(
            {"status": "error", "change": change.to_dict()}, status_code=400
        )


@app.get("/get_full_changelog")
async def get_full_changelog():
    changelog = []
    for filename in sorted(os.listdir(CHANGELOG_FOLDER)):
        try:
            with open(os.path.join(CHANGELOG_FOLDER, filename), "r") as f:
                changelog.append(json.load(f))
        except Exception:
            print(f"Skipping file: {filename}")
    return JSONResponse(changelog)


@app.get("/get_file/{filename:path}")
async def get_file(filename: str):
    file_path = os.path.join(SNAPSHOT_FOLDER, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    else:
        raise HTTPException(status_code=404, detail="File not found")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run FastAPI server")
    parser.add_argument(
        "--port",
        type=int,
        default=5001,
        help="Port to run the server on (default: 5001)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Run the server in debug mode with hot reloading",
    )

    args = parser.parse_args()

    uvicorn.run(
        "server:app" if args.debug else app,  # Use import string in debug mode
        host="0.0.0.0",
        port=args.port,
        log_level="debug" if args.debug else "info",
        reload=args.debug,  # Enable hot reloading only in debug mode
    )
