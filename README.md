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
- The script was written and tested against internxt CLI version 1.5.4.
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

This is copy-pasted from the internxt reference, plus actual output json, so an AI can reason about this properly.

<!-- commands -->
* [`internxt add-cert`](#internxt-add-cert)
* [`internxt config`](#internxt-config)
* [`internxt create-folder`](#internxt-create-folder)
* [`internxt delete-permanently-file`](#internxt-delete-permanently-file)
* [`internxt delete-permanently-folder`](#internxt-delete-permanently-folder)
* [`internxt delete permanently file`](#internxt-delete-permanently-file)
* [`internxt delete permanently folder`](#internxt-delete-permanently-folder)
* [`internxt download-file`](#internxt-download-file)
* [`internxt download file`](#internxt-download-file)
* [`internxt list`](#internxt-list)
* [`internxt login`](#internxt-login)
* [`internxt logout`](#internxt-logout)
* [`internxt logs`](#internxt-logs)
* [`internxt move-file`](#internxt-move-file)
* [`internxt move-folder`](#internxt-move-folder)
* [`internxt move file`](#internxt-move-file)
* [`internxt move folder`](#internxt-move-folder)
* [`internxt rename-file`](#internxt-rename-file)
* [`internxt rename-folder`](#internxt-rename-folder)
* [`internxt rename file`](#internxt-rename-file)
* [`internxt rename folder`](#internxt-rename-folder)
* [`internxt trash-clear`](#internxt-trash-clear)
* [`internxt trash-file`](#internxt-trash-file)
* [`internxt trash-folder`](#internxt-trash-folder)
* [`internxt trash-list`](#internxt-trash-list)
* [`internxt trash-restore-file`](#internxt-trash-restore-file)
* [`internxt trash-restore-folder`](#internxt-trash-restore-folder)
* [`internxt trash clear`](#internxt-trash-clear)
* [`internxt trash file`](#internxt-trash-file)
* [`internxt trash folder`](#internxt-trash-folder)
* [`internxt trash list`](#internxt-trash-list)
* [`internxt trash restore file`](#internxt-trash-restore-file)
* [`internxt trash restore folder`](#internxt-trash-restore-folder)
* [`internxt upload-file`](#internxt-upload-file)
* [`internxt upload file`](#internxt-upload-file)
* [`internxt webdav ACTION`](#internxt-webdav-action)
* [`internxt webdav-config`](#internxt-webdav-config)
* [`internxt whoami`](#internxt-whoami)

## `internxt add-cert`

Add a self-signed certificate to the trusted store for macOS, Linux, and Windows.

```
USAGE
  $ internxt add-cert [--json]

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Add a self-signed certificate to the trusted store for macOS, Linux, and Windows.

EXAMPLES
  $ internxt add-cert
```

_See code: [src/commands/add-cert.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/add-cert.ts)_

## `internxt config`

Display useful information from the user logged into the Internxt CLI.

```
USAGE
  $ internxt config [--json]

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Display useful information from the user logged into the Internxt CLI.

EXAMPLES
  $ internxt config
```

_See code: [src/commands/config.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/config.ts)_

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
    "message": "Folder 2020 created successfully, view it at https://drive.internxt.com/folder/b499f304-a55b-4aa5-8759-1c3466ce76b7",
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

_See code: [src/commands/create-folder.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/create-folder.ts)_

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

ALIASES
  $ internxt delete permanently file

EXAMPLES
  $ internxt delete-permanently-file
```

_See code: [src/commands/delete-permanently-file.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/delete-permanently-file.ts)_

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

ALIASES
  $ internxt delete permanently folder

EXAMPLES
  $ internxt delete-permanently-folder
```

_See code: [src/commands/delete-permanently-folder.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/delete-permanently-folder.ts)_

## `internxt delete permanently file`

Deletes permanently a file. This action cannot be undone.

```
USAGE
  $ internxt delete permanently file [--json] [-x] [-i <value>]

FLAGS
  -i, --id=<value>  The file id to be permanently deleted.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Deletes permanently a file. This action cannot be undone.

ALIASES
  $ internxt delete permanently file

EXAMPLES
  $ internxt delete permanently file
```

## `internxt delete permanently folder`

Deletes permanently a folder. This action cannot be undone.

```
USAGE
  $ internxt delete permanently folder [--json] [-x] [-i <value>]

FLAGS
  -i, --id=<value>  The folder id to be permanently deleted.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Deletes permanently a folder. This action cannot be undone.

ALIASES
  $ internxt delete permanently folder

EXAMPLES
  $ internxt delete permanently folder
```

## `internxt download-file`

Download and decrypts a file from Internxt Drive to a directory. The file name will be the same as the file name in your Drive.

```
USAGE
  $ internxt download-file [--json] [-x] [-i <value>] [-d <value>] [-o]

FLAGS
  -d, --directory=<value>  The directory to download the file to. Leave empty for the current folder.
  -i, --id=<value>         The id of the file to download. Use internxt list to view your files ids
  -o, --overwrite          Overwrite the file if it already exists

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Download and decrypts a file from Internxt Drive to a directory. The file name will be the same as the file name in
  your Drive.

ALIASES
  $ internxt download file

EXAMPLES
  $ internxt download-file
```

_See code: [src/commands/download-file.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/download-file.ts)_

## `internxt download file`

Download and decrypts a file from Internxt Drive to a directory. The file name will be the same as the file name in your Drive.

```
USAGE
  $ internxt download file [--json] [-x] [-i <value>] [-d <value>] [-o]

FLAGS
  -d, --directory=<value>  The directory to download the file to. Leave empty for the current folder.
  -i, --id=<value>         The id of the file to download. Use internxt list to view your files ids
  -o, --overwrite          Overwrite the file if it already exists

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Download and decrypts a file from Internxt Drive to a directory. The file name will be the same as the file name in
  your Drive.

ALIASES
  $ internxt download file

EXAMPLES
  $ internxt download file
```

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
        },
        {
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
          "createdAt": "2025-08-06T08:21:39.000Z",
          "updatedAt": "2025-08-06T08:21:39.000Z",
          "uuid": "<uuid>",
          "plainName": "testfolder2",
          "size": 0,
          "removed": false,
          "removedAt": null,
          "sharings": [],
          "creationTime": "2025-08-06T08:21:39.170Z",
          "modificationTime": "2025-08-06T08:21:39.170Z",
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

_See code: [src/commands/list.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/list.ts)_

## `internxt login`

Logs into an Internxt account. If the account is two-factor protected, then an extra code will be required.

```
USAGE
  $ internxt login [--json] [-x] [-e <value>] [-p <value>] [-w 123456]

FLAGS
  -e, --email=<value>     The email to log in
  -p, --password=<value>  The plain password to log in
  -w, --twofactor=123456  The two factor auth code (only needed if the account is two-factor protected)

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Logs into an Internxt account. If the account is two-factor protected, then an extra code will be required.

EXAMPLES
  $ internxt login
```

_See code: [src/commands/login.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/login.ts)_

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
  $ internxt logout
```

_See code: [src/commands/logout.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/logout.ts)_

## `internxt logs`

Displays the Internxt CLI logs directory path

```
USAGE
  $ internxt logs [--json]

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Displays the Internxt CLI logs directory path

EXAMPLES
  $ internxt logs
```

_See code: [src/commands/logs.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/logs.ts)_

## `internxt move-file`

Move a file into a destination folder.

```
USAGE
  $ internxt move-file [--json] [-x] [-i <value>] [-d <value>]

FLAGS
  -d, --destination=<value>  The destination folder id where the file is going to be moved. Leave empty for the root
                             folder.
  -i, --id=<value>           The ID of the file to be moved.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Move a file into a destination folder.

ALIASES
  $ internxt move file

EXAMPLES
  $ internxt move-file
```

_See code: [src/commands/move-file.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/move-file.ts)_

## `internxt move-folder`

Move a folder into a destination folder.

```
USAGE
  $ internxt move-folder [--json] [-x] [-i <value>] [-d <value>]

FLAGS
  -d, --destination=<value>  The destination folder id where the folder is going to be moved. Leave empty for the root
                             folder.
  -i, --id=<value>           The ID of the folder to be moved.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Move a folder into a destination folder.

ALIASES
  $ internxt move folder

EXAMPLES
  $ internxt move-folder
```

_See code: [src/commands/move-folder.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/move-folder.ts)_

## `internxt move file`

Move a file into a destination folder.

```
USAGE
  $ internxt move file [--json] [-x] [-i <value>] [-d <value>]

FLAGS
  -d, --destination=<value>  The destination folder id where the file is going to be moved. Leave empty for the root
                             folder.
  -i, --id=<value>           The ID of the file to be moved.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Move a file into a destination folder.

ALIASES
  $ internxt move file

EXAMPLES
  $ internxt move file
```

## `internxt move folder`

Move a folder into a destination folder.

```
USAGE
  $ internxt move folder [--json] [-x] [-i <value>] [-d <value>]

FLAGS
  -d, --destination=<value>  The destination folder id where the folder is going to be moved. Leave empty for the root
                             folder.
  -i, --id=<value>           The ID of the folder to be moved.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Move a folder into a destination folder.

ALIASES
  $ internxt move folder

EXAMPLES
  $ internxt move folder
```

## `internxt rename-file`

Rename a file.

```
USAGE
  $ internxt rename-file [--json] [-x] [-i <value>] [-n <value>]

FLAGS
  -i, --id=<value>    The ID of the file to be renamed.
  -n, --name=<value>  The new name for the file.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Rename a file.

ALIASES
  $ internxt rename file

EXAMPLES
  $ internxt rename-file
```

_See code: [src/commands/rename-file.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/rename-file.ts)_

## `internxt rename-folder`

Rename a folder.

```
USAGE
  $ internxt rename-folder [--json] [-x] [-i <value>] [-n <value>]

FLAGS
  -i, --id=<value>    The ID of the folder to be renamed.
  -n, --name=<value>  The new name for the folder.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Rename a folder.

ALIASES
  $ internxt rename folder

EXAMPLES
  $ internxt rename-folder
```

_See code: [src/commands/rename-folder.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/rename-folder.ts)_

## `internxt rename file`

Rename a file.

```
USAGE
  $ internxt rename file [--json] [-x] [-i <value>] [-n <value>]

FLAGS
  -i, --id=<value>    The ID of the file to be renamed.
  -n, --name=<value>  The new name for the file.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Rename a file.

ALIASES
  $ internxt rename file

EXAMPLES
  $ internxt rename file
```

## `internxt rename folder`

Rename a folder.

```
USAGE
  $ internxt rename folder [--json] [-x] [-i <value>] [-n <value>]

FLAGS
  -i, --id=<value>    The ID of the folder to be renamed.
  -n, --name=<value>  The new name for the folder.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Rename a folder.

ALIASES
  $ internxt rename folder

EXAMPLES
  $ internxt rename folder
```

## `internxt trash-clear`

Deletes permanently all the content of the trash. This action cannot be undone.

```
USAGE
  $ internxt trash-clear [--json] [-x] [-f]

FLAGS
  -f, --force  It forces the trash to be emptied without confirmation.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Deletes permanently all the content of the trash. This action cannot be undone.

ALIASES
  $ internxt trash clear

EXAMPLES
  $ internxt trash-clear
```

_See code: [src/commands/trash-clear.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/trash-clear.ts)_

## `internxt trash-file`

Moves a given file to the trash.

```
USAGE
  $ internxt trash-file [--json] [-x] [-i <value>]

FLAGS
  -i, --id=<value>  The file id to be trashed.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Moves a given file to the trash.

ALIASES
  $ internxt trash file

EXAMPLES
  $ internxt trash-file
```

_See code: [src/commands/trash-file.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/trash-file.ts)_

## `internxt trash-folder`

Moves a given folder to the trash.

```
USAGE
  $ internxt trash-folder [--json] [-x] [-i <value>]

FLAGS
  -i, --id=<value>  The folder id to be trashed.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Moves a given folder to the trash.

ALIASES
  $ internxt trash folder

EXAMPLES
  $ internxt trash-folder
```

_See code: [src/commands/trash-folder.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/trash-folder.ts)_

## `internxt trash-list`

Lists the content of the trash.

```
USAGE
  $ internxt trash-list [--json] [-e]

FLAGS
  -e, --extended  Displays additional information in the trash list.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Lists the content of the trash.

ALIASES
  $ internxt trash list

EXAMPLES
  $ internxt trash-list
```

_See code: [src/commands/trash-list.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/trash-list.ts)_

## `internxt trash-restore-file`

Restore a trashed file into a destination folder.

```
USAGE
  $ internxt trash-restore-file [--json] [-x] [-i <value>] [-d <value>]

FLAGS
  -d, --destination=<value>  The folder id where the file is going to be restored. Leave empty for the root folder.
  -i, --id=<value>           The file id to be restored from the trash.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Restore a trashed file into a destination folder.

ALIASES
  $ internxt trash restore file

EXAMPLES
  $ internxt trash-restore-file
```

_See code: [src/commands/trash-restore-file.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/trash-restore-file.ts)_

## `internxt trash-restore-folder`

Restore a trashed folder into a destination folder.

```
USAGE
  $ internxt trash-restore-folder [--json] [-x] [-i <value>] [-d <value>]

FLAGS
  -d, --destination=<value>  The folder id where the folder is going to be restored. Leave empty for the root folder.
  -i, --id=<value>           The folder id to be restored from the trash.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Restore a trashed folder into a destination folder.

ALIASES
  $ internxt trash restore folder

EXAMPLES
  $ internxt trash-restore-folder
```

_See code: [src/commands/trash-restore-folder.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/trash-restore-folder.ts)_

## `internxt trash clear`

Deletes permanently all the content of the trash. This action cannot be undone.

```
USAGE
  $ internxt trash clear [--json] [-x] [-f]

FLAGS
  -f, --force  It forces the trash to be emptied without confirmation.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Deletes permanently all the content of the trash. This action cannot be undone.

ALIASES
  $ internxt trash clear

EXAMPLES
  $ internxt trash clear
```

## `internxt trash file`

Moves a given file to the trash.

```
USAGE
  $ internxt trash file [--json] [-x] [-i <value>]

FLAGS
  -i, --id=<value>  The file id to be trashed.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Moves a given file to the trash.

ALIASES
  $ internxt trash file

EXAMPLES
  $ internxt trash file
```

## `internxt trash folder`

Moves a given folder to the trash.

```
USAGE
  $ internxt trash folder [--json] [-x] [-i <value>]

FLAGS
  -i, --id=<value>  The folder id to be trashed.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Moves a given folder to the trash.

ALIASES
  $ internxt trash folder

EXAMPLES
  $ internxt trash folder
```

## `internxt trash list`

Lists the content of the trash.

```
USAGE
  $ internxt trash list [--json] [-e]

FLAGS
  -e, --extended  Displays additional information in the trash list.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Lists the content of the trash.

ALIASES
  $ internxt trash list

EXAMPLES
  $ internxt trash list
```

## `internxt trash restore file`

Restore a trashed file into a destination folder.

```
USAGE
  $ internxt trash restore file [--json] [-x] [-i <value>] [-d <value>]

FLAGS
  -d, --destination=<value>  The folder id where the file is going to be restored. Leave empty for the root folder.
  -i, --id=<value>           The file id to be restored from the trash.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Restore a trashed file into a destination folder.

ALIASES
  $ internxt trash restore file

EXAMPLES
  $ internxt trash restore file
```

## `internxt trash restore folder`

Restore a trashed folder into a destination folder.

```
USAGE
  $ internxt trash restore folder [--json] [-x] [-i <value>] [-d <value>]

FLAGS
  -d, --destination=<value>  The folder id where the folder is going to be restored. Leave empty for the root folder.
  -i, --id=<value>           The folder id to be restored from the trash.

HELPER FLAGS
  -x, --non-interactive  Prevents the CLI from being interactive. When enabled, the CLI will not request input through
                         the console and will throw errors directly.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Restore a trashed folder into a destination folder.

ALIASES
  $ internxt trash restore folder

EXAMPLES
  $ internxt trash restore folder
```

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

ALIASES
  $ internxt upload file

EXAMPLES
  $ internxt upload-file

EXAMPLE OUTPUT
{
  "success": true,
  "message": "File uploaded in 4175ms, view it at https://drive.internxt.com/file/2c424dda-16a2-4d35-9c33-86621723a360",
  "file": {
    "name": "P1200047",
    "encryptedName": "ONzgORtJ77qI28jDnr+GjwJn6xELsAEqsn3FKlKNYbHR7Z129AD/WOMkAChEKx6rm7hOER2drdmXmC296dvSXtE5y5os0XCS554YYc+dcCMlxieIgBKp9725x8m9WaY2z273SJkGy+U=",
    "id": 123456789,
    "uuid": "<uuid>",
    "size": "3185391",
    "bucket": "<bucketid>",
    "createdAt": "2025-08-09T06:43:43.733Z",
    "updatedAt": "2025-08-09T06:43:44.000Z",
    "fileId": "<fileid>",
    "type": "RW2",
    "status": "EXISTS",
    "folderId": <folderid>,
    "folderUuid": "<uuid>"
  }
}

EXAMPLE OUTPUT
{"success":false,"message":"File already exists"}
```

_See code: [src/commands/upload-file.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/upload-file.ts)_

## `internxt upload file`

Upload a file to Internxt Drive

```
USAGE
  $ internxt upload file [--json] [-x] [-f <value>] [-i <value>]

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

ALIASES
  $ internxt upload file

EXAMPLES
  $ internxt upload file
```

## `internxt webdav ACTION`

Enable, disable, restart or get the status of the Internxt CLI WebDav server

```
USAGE
  $ internxt webdav ACTION [--json]

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Enable, disable, restart or get the status of the Internxt CLI WebDav server

EXAMPLES
  $ internxt webdav enable

  $ internxt webdav disable

  $ internxt webdav restart

  $ internxt webdav status
```

_See code: [src/commands/webdav.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/webdav.ts)_

## `internxt webdav-config`

Edit the configuration of the Internxt CLI WebDav server as the port or the protocol.

```
USAGE
  $ internxt webdav-config [--json] [-p <value>] [-s | -h] [-t <value>]

FLAGS
  -h, --http             Configures the WebDAV server to use insecure plain HTTP.
  -p, --port=<value>     The new port for the WebDAV server.
  -s, --https            Configures the WebDAV server to use HTTPS with self-signed certificates.
  -t, --timeout=<value>  Configures the WebDAV server to use this timeout in minutes.

GLOBAL FLAGS
  --json  Format output as json.

DESCRIPTION
  Edit the configuration of the Internxt CLI WebDav server as the port or the protocol.

EXAMPLES
  $ internxt webdav-config
```

_See code: [src/commands/webdav-config.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/webdav-config.ts)_

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
  $ internxt whoami
```

_See code: [src/commands/whoami.ts](https://github.com/internxt/cli/blob/v1.5.4/src/commands/whoami.ts)_
