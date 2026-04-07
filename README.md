# internxt_backup
Backup/sync script that uses the internxt cloud storage CLI.

## About

This script uses the internxt CLI interface to back up / sync local files to an internxt cloud storage account.

This was written to have a comfortable, fully featured, automated, and robust backup utility.
The internxt service frequently has connectivity issues that are not handled well by the web client, which also has problems when uploading many files and does not give any feedback while uploading.

## Features
- The script creates the same folder structure in the cloud storage that exists locally, except for folders that contain a .internxtignore file.
- The script does not upload a source file that already exists and has the same size (the CLI doesn't allow this anyway).
- The script optionally (`--allow-delete`, `-d`) removes folders and files that are in the target but not in the source (or contain .internxtignore files).
- The script optionally (`--allow-delete`, `-d`) removes files that exist in the target but have a different size (meaning they get re-uploaded).
- The script copies all files that do not exist remotely from source to target.
- If a CLI command fails, the script automatically attempts up to 5 retries at increasing time intervals (configurable with `--max_num_retries N` and `--retry_wait_seconds M`).
- If a CLI command fails all retries, the file is skipped.
- All actions are logged to a log file. Some output such as a progress bar and summaries are also written to stdout.
- If the script for some reason is stopped or crashes, the same command line can just be issued again and it will by definition of how it works resume where the last command stopped.
- The script was written and tested against internxt CLI version 1.6.3.
- The script was written and tested on Windows 11 with node-v22.18.0-win-x64 and on Ubuntu Server 24.04 with node v22.19.0.

## Usage
The script requires the internxt CLI ("internxt.cmd" or "internxt") to be available in the shell. It also requires the node framework to be available.

### Examples 

```bash
# Example including PATH setup and login/logout
set PATH=C:\Users\Me\node-v22.18.0-win-x64;%PATH%
internxt login -e me@me.com
python C:\Users\Me\internxt_backup\internxt_backup.py -s C:\Users\Me\pictures -t "" -d
internxt logout

# Example with automatic login/logout (password requested interactively)
# This is known to not work properly in git bash, use -p or log in/out manually instead!
# Windows cmd
set PATH=C:\Users\Me\node-v22.18.0-win-x64;%PATH%
python C:\Users\Me\internxt_backup\internxt_backup.py -e me@me.com -s C:\Users\Me\pictures -t "" -d
# Linux
export PATH=/usr/bin/:$PATH
python ~/internxt_backup/internxt_backup.py -e me@me.com -s ~/pictures/ -t "" -d

# Basic usage (upload to root folder)
python internxt_backup.py --source /path/to/source --target ""

# Sync source folder (= upload missing files, re-upload files with different size,
# delete files/folders that don't exist locally)
python internxt_backup.py --source /path/to/source --target "" --allow-delete

# Upload to specific folder ID (easiest to log into web client, navigate to folder, copy from URL)
python internxt_backup.py --source /path/to/source --target "12345678-abcd-efgh-90abcdef"

# Log everything to console in addition to logging to file (default is to log everything to file
# and only some parts to console)
python internxt_backup.py --source /path/to/source --target "" --full-console-log

# Verbose logging (logs every command; known to have some issues)
python internxt_backup.py --source /path/to/source --target "" --verbose
```

## Known Issues
- Password prompt does not work in git bash, causes a hang. Use -p or log in/out manually.
- Some files with special characters will upload fine but in subsequent runs fail to be recognized as existing files. This leads to repeated failed/skipped uploads, but the files are there.
- I've encountered one weird case where uploading an entire folder would fail 2 random files (different ones when trying to re-upload the entire folder!). In subsequent runs, these files are reported to exist, but they don't appear in the web client. I don't think this is a problem with this script but with internxt. I have not reached out to them yet.

# Internxt Resources

* CLI Reference: https://raw.githubusercontent.com/internxt/cli/refs/heads/main/README.md
* Additional information: https://help.internxt.com/en/articles/9178044-does-internxt-support-webdav

# Internxt CLI Commands

Only the commands used by internxt_backup.py are listed here.
Written against @internxt/cli/1.6.3.
Copy-pasted from the internxt reference plus actual output JSON, so an AI can reason about this properly.

<!-- commands -->
* [`internxt whoami`](#internxt-whoami)
* [`internxt login-legacy`](#internxt-login-legacy)
* [`internxt logout`](#internxt-logout)
* [`internxt list`](#internxt-list)
* [`internxt create-folder`](#internxt-create-folder)
* [`internxt delete-permanently-file`](#internxt-delete-permanently-file)
* [`internxt delete-permanently-folder`](#internxt-delete-permanently-folder)
* [`internxt upload-file`](#internxt-upload-file)

## `internxt whoami`

Display the current user logged into the Internxt CLI.

```
USAGE
  $ internxt whoami [--json]

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Display the current user logged into the Internxt CLI.

EXAMPLES
  $ internxt whoami --json -x

EXAMPLE OUTPUT
  {
    "success": true,
    "message": "You are logged in as: <email>.",
    "login": {
      "user": {
        "email": "<email>",
        "userId": "<userId-hash>",
        "mnemonic": "<mnemonic>",
        "root_folder_id": 123456789,
        "rootFolderId": "<uuid>",
        "name": "My",
        "lastname": "Internxt",
        "uuid": "<uuid>",
        "credit": 0,
        "createdAt": "2025-05-26T09:54:23.000Z",
        "privateKey": "<privateKey>",
        "publicKey": "<publicKey>",
        "revocateKey": "<revocateKey>",
        "tierId": "<uuid>",
        "keys": {
          "ecc": {
            "privateKey": "<privateKey>",
            "publicKey": "<publicKey>"
          },
          "kyber": {
            "privateKey": "<privateKey>",
            "publicKey": "<publicKey>"
          }
        },
        "bucket": "<bucketid>",
        "registerCompleted": true,
        "teams": false,
        "username": "<email>",
        "bridgeUser": "<email>",
        "sharedWorkspace": false,
        "appSumoDetails": null,
        "hasReferralsProgram": false,
        "backupsBucket": "<bucketid>",
        "avatar": null,
        "emailVerified": true,
        "lastPasswordChangedAt": "2026-02-13T19:40:00.000Z"
      },
      "token": "<jwt-token>"
    }
  }
```

_See code: [src/commands/whoami.ts](https://github.com/internxt/cli/blob/v1.6.3/src/commands/whoami.ts)_

## `internxt login-legacy`

Logs into an Internxt account using legacy authentication (required for accounts without two-factor).

```
USAGE
  $ internxt login-legacy [--json] [-e <value>] [-p <value>]

FLAGS
  -e, --email=<value>     The email to log in
  -p, --password=<value>  The plain password to log in

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Logs into an Internxt account using legacy authentication (required for accounts without two-factor).

EXAMPLES
  $ internxt login-legacy -e me@example.com --json

EXAMPLE OUTPUT
  {
    "success": true,
    "message": "Succesfully logged in to: <email>.",
    "login": {
      "user": {
        "email": "<email>",
        "userId": "<userId-hash>",
        "mnemonic": "<mnemonic>",
        "root_folder_id": 123456789,
        "rootFolderId": "<uuid>",
        "name": "My",
        "lastname": "Internxt",
        "uuid": "<uuid>",
        "credit": 0,
        "createdAt": "2025-05-26T09:54:23.000Z",
        "privateKey": "<privateKey>",
        "publicKey": "<publicKey>",
        "revocateKey": "<revocateKey>",
        "tierId": "<uuid>",
        "keys": {
          "ecc": {
            "privateKey": "<privateKey>",
            "publicKey": "<publicKey>"
          },
          "kyber": {
            "privateKey": "<privateKey>",
            "publicKey": "<publicKey>"
          }
        },
        "bucket": "<bucketid>",
        "registerCompleted": true,
        "teams": false,
        "username": "<email>",
        "bridgeUser": "<email>",
        "sharedWorkspace": false,
        "appSumoDetails": null,
        "hasReferralsProgram": false,
        "backupsBucket": "<bucketid>",
        "avatar": null,
        "emailVerified": true,
        "lastPasswordChangedAt": "2026-02-13T19:40:00.000Z"
      },
      "token": "<jwt-token>"
    }
  }
```

_See code: [src/commands/login-legacy.ts](https://github.com/internxt/cli/blob/v1.6.3/src/commands/login-legacy.ts)_

## `internxt logout`

Logs out the current internxt user that is logged into the Internxt CLI.

```
USAGE
  $ internxt logout [--json]

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Logs out the current internxt user that is logged into the Internxt CLI.

EXAMPLES
  $ internxt logout --json

EXAMPLE OUTPUT
  {
    "success": true,
    "message": "User logged out successfully."
  }
```

_See code: [src/commands/logout.ts](https://github.com/internxt/cli/blob/v1.6.3/src/commands/logout.ts)_

## `internxt list`

Lists the content of a folder id.

```
USAGE
  $ internxt list [--json] [-x] [-i <value>] [-e]

FLAGS
  -e, --extended    Displays additional information in the list.
  -i, --id=<value>  The folder id to list. Leave empty for the root folder.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Lists the content of a folder id.

EXAMPLES
  $ internxt list --json

EXAMPLE OUTPUT
  {
    "success": true,
    "list": {
      "folders": [
        {
          "type": "folder",
          "id": 118633976,
          "parentId": 123456789,
          "parentUuid": "<uuid>",
          "name": "<verylonghash>",
          "parent": null,
          "bucket": null,
          "userId": 1234567,
          "user": null,
          "encryptVersion": "03-aes",
          "deleted": false,
          "deletedAt": null,
          "createdAt": "2025-05-26T09:54:23.000Z",
          "updatedAt": "2025-05-26T09:54:23.000Z",
          "uuid": "<uuid>",
          "plainName": "testfolder1",
          "size": 0,
          "removed": false,
          "removedAt": null,
          "sharings": [],
          "creationTime": "2025-05-26T09:54:22.894Z",
          "modificationTime": "2025-05-26T09:54:22.894Z",
          "status": "EXISTS"
        }
      ],
      "files": [
      {
        "id": 123456789,
        "fileId": "<fileid>",
        "folderId": <folderid>,
        "folder": null,
        "name": "<verylonghash>",
        "type": "txt",
        "size": "85",
        "bucket": "<bucketid>",
        "encryptVersion": "03-aes",
        "deleted": false,
        "deletedAt": null,
        "userId": 1234567,
        "user": null,
        "creationTime": "2025-08-07T21:06:24.000Z",
        "modificationTime": "2025-08-07T21:06:24.000Z",
        "createdAt": "2025-08-07T21:06:23.733Z",
        "updatedAt": "2025-08-07T21:06:24.000Z",
        "folderUuid": "<uuid>",
        "uuid": "<uuid>",
        "plainName": "testfile",
        "removed": false,
        "removedAt": null,
        "status": "EXISTS",
        "thumbnails": [],
        "sharings": []
      }
    ]
    }
  }
```

_See code: [src/commands/list.ts](https://github.com/internxt/cli/blob/v1.6.3/src/commands/list.ts)_

## `internxt create-folder`

Create a folder in your Internxt Drive

```
USAGE
  $ internxt create-folder [--json] [-x] [-n <value>] [-i <value>]

FLAGS
  -i, --id=<value>    The ID of the folder where the new folder will be created. Defaults to your root folder if not
                      specified.
  -n, --name=<value>  The new name for the folder

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Create a folder in your Internxt Drive

EXAMPLES
  $ internxt create-folder

EXAMPLE OUTPUT
  {
    "success": true,
    "message": "Folder testfolder created successfully, view it at https://drive.internxt.com/folder/b499f804-af5b-4aa5-8059-1c346cce76b7",
    "folder": {
      "type": "folder",
      "id": 123456789,
      "parentId": 123456789,
      "parentUuid": "<uuid>",
      "name": "<verylonghash>",
      "parent": null,
      "bucket": null,
      "userId": 1234567,
      "user": null,
      "encryptVersion": "03-aes",
      "deleted": false,
      "deletedAt": null,
      "createdAt": "2025-08-08T07:38:28.000Z",
      "updatedAt": "2025-08-08T07:38:28.000Z",
      "uuid": "<uuid>",
      "plainName": "testfolder",
      "size": 0,
      "removed": false,
      "removedAt": null,
      "creationTime": "2025-08-08T07:38:27.978Z",
      "modificationTime": "2025-08-08T07:38:27.978Z",
      "status": "EXISTS"
    }
  }
```

_See code: [src/commands/create-folder.ts](https://github.com/internxt/cli/blob/v1.6.3/src/commands/create-folder.ts)_

## `internxt delete-permanently-file`

Deletes permanently a file. This action cannot be undone.

```
USAGE
  $ internxt delete-permanently-file [--json] [-x] [-i <value>]

FLAGS
  -i, --id=<value>  The file id to be permanently deleted.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Deletes permanently a file. This action cannot be undone.

EXAMPLES
  $ internxt delete-permanently-file --id=<file-uuid> --json -x

EXAMPLE OUTPUT
  {
    "success": true,
    "message": "File permanently deleted successfully"
  }
```

_See code: [src/commands/delete-permanently-file.ts](https://github.com/internxt/cli/blob/v1.6.3/src/commands/delete-permanently-file.ts)_

## `internxt delete-permanently-folder`

Deletes permanently a folder. This action cannot be undone.

```
USAGE
  $ internxt delete-permanently-folder [--json] [-x] [-i <value>]

FLAGS
  -i, --id=<value>  The folder id to be permanently deleted.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Deletes permanently a folder. This action cannot be undone.

EXAMPLES
  $ internxt delete-permanently-folder --id=<folder-uuid> --json -x

EXAMPLE OUTPUT
  {
    "success": true,
    "message": "Folder permanently deleted successfully"
  }
```

_See code: [src/commands/delete-permanently-folder.ts](https://github.com/internxt/cli/blob/v1.6.3/src/commands/delete-permanently-folder.ts)_

## `internxt upload-file`

Upload a file to Internxt Drive

```
USAGE
  $ internxt upload-file [--json] [-x] [-f <value>] [-i <value>]

FLAGS
  -f, --file=<value>         The path to the file on your system.
  -i, --destination=<value>  The folder id where the file is going to be uploaded to. Leave empty for the root folder.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Upload a file to Internxt Drive

EXAMPLES
  $ internxt upload-file

EXAMPLE OUTPUT
{
  "success": true,
  "message": "File uploaded successfully in 00:00:01.234, view it at https://drive.internxt.com/file/<uuid>",
  "file": {
    "itemType": "file",
    "name": "myfile",
    "uuid": "<uuid>",
    "size": "3185391",
    "bucket": "<bucketid>",
    "createdAt": "2026-04-06T12:43:31.158Z",
    "updatedAt": "2026-04-06T12:43:31.000Z",
    "fileId": "<fileid>",
    "type": "jpg",
    "status": "EXISTS",
    "folderUuid": "<uuid>",
    "creationTime": "2026-04-06T12:43:29.000Z",
    "modificationTime": "2026-04-06T12:43:29.000Z",
    "plainName": "myfile"
  }
}

EXAMPLE OUTPUT
{"success":false,"message":"File already exists"}
```

_See code: [src/commands/upload-file.ts](https://github.com/internxt/cli/blob/v1.6.3/src/commands/upload-file.ts)_
