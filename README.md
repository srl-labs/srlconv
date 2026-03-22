# srlconv - SR Linux Configuration Converter and Diff Tool

Convert Nokia SR Linux configuration between software versions and compare representations before and after.

Non backward compatible changes may be introduced in new major SR Linux releases (e.g. 24.XX.Y -> 25.XX.Y) with the scope of these changes covered in the corresponding SR Linux Release Notes.  
The SR Linux Network OS handles the translation between the old an new configuration automatically when performing software upgrades on production hardware. However, automation tools, scripts and templates must be updated manually to reflect the changes made to the underlying configuration and/or state model.

`srlconv` tool helps you identify the changes for your existing SR Linux configuration when migrating to a new major release by offering the following features:

- Convert your current configuration between versions and storing the new config in json, cli and cli-flat formats.
- Computing the diff between the current and new configuration in json, cli and cli-flat formats.

<https://github.com/user-attachments/assets/db829387-8c42-41f7-9a0d-67129cbc2529>

## Prerequisites

- `curl` to download and execute the installation script.
- [`containerlab`](https://containerlab.dev/) to deploy the SR Linux containers.

## Installation

Install with a single command:

```bash
curl -sSL eda.dev/uvx | \
sh -s -- https://github.com/srl-labs/srlconv/archive/refs/heads/main.zip
```

## Usage

To convert your current configuration between versions, use the `convert` subcommand and provide the current and target versions and the path to the configuration file used as startup config on the current node.  
The command will create a temporary workspace directory and deploy the lab, run configuration conversion, and write artifacts under the workspace `output/` directory.

```bash
srlconv convert \
  --current-version 25.10.1 \
  --current-config my-current.cfg \
  --target-version 25.10.2
```

> The current configuration can be provided in json, cli, or cli-flat formats as supported by SR Linux for `startup-config`.

The tool will create a temporary workspace directory and deploy the lab, run configuration conversion, and write artifacts under the workspace's `output/` directory, e.g. `/tmp/srlconv-1234567890/output`.

At the end of the command execution, the tool will print a table with distinct paths for the current and target configuration in json, cli and cli-flat formats. Additionally, it will print a prompt to open the DeepDiff comparisons for the JSON, CLI, and CLI-flat configurations.

| Option                     | Required | Default                  | Description                                                                            |
| -------------------------- | -------- | ------------------------ | -------------------------------------------------------------------------------------- |
| `--current-version`        | yes      | —                        | Source SR Linux version (e.g. `25.10.1`).                                |
| `--current-config`         | yes      | —                        | Path to the configuration file used as startup config on **srl-current** (must exist). |
| `--current-type`           | no       | `ixr-d2l`                | SR Linux **type** for the current node.                                                |
| `--target-version`         | yes      | —                        | Target SR Linux version. (e.g. `26.3.1`)                                                               |
| `--target-type`            | no       | same as `--current-type` | SR Linux **type** for the target node.                                                 |

If you have a hardware node, extract its running config and provide it as the `--current-config` argument.

## Using the Diff

The prompt at the end of the command execution will allow you to display the DeepDiff comparisons for the JSON, CLI, and CLI-flat configurations. This diff should ultimately help you identify the changes for your existing configuration when migrating to a new major release.

The diff can be displayed in three different formats:

- JSON
- CLI
- CLI-flat

> Start with the JSON diff, as it is the most detailed, and check the CLI and CLI-flat if needed.

For example, here is a JSON diff between some sample config between versions 25.10.1 and 26.3.1:

```json
{
  "dictionary_item_added": [
    "root['srl_nokia-system:system']['srl_nokia-dns:dns-instance']"
  ],
  "dictionary_item_removed": [
    "root['srl_nokia-system:system']['srl_nokia-dns:dns']"
  ],
  "values_changed": {
    "root['srl_nokia-system:system']['srl_nokia-tls:tls']": {
      "new_value": {
        "profile": [
          {
            "name": "clab-profile",
            "key": 
"$aes1$something",
            "certificate": "-----BEGIN CERTIFICATE-----...",
            "authenticate-client": false
          }
        ]
      },
      "old_value": {
        "server-profile": [
          {
            "name": "clab-profile",
            "key": 
"$aes1$something",
            "certificate": "-----BEGIN CERTIFICATE-----...",
            "authenticate-client": false
          }
        ]
      }
    }
  }
}
```

Look at the `dictionary_item_added`, `dictionary_item_removed` and `values_changed` keys to identify the changes.

Based on the diff above, we can see that `root['srl_nokia-system:system']['srl_nokia-tls:tls']` now has a new value `profile` that was previously a `server-profile`. This is a renaming of the container from `server-profile` to `profile`.

Based on the `dictionary_item_added` and `dictionary_item_removed` keys, we can see that the `root['srl_nokia-system:system']['srl_nokia-dns:dns']` container was removed and a new one called `root['srl_nokia-system:system']['srl_nokia-dns:dns-instance']` was added. This is a change from the single instance `dns` container to a new `dns-instance` list.

When looking at the CLI-Flat diff, we spot the same, however ordering of the items may confuse the diff tool.

## Generated outputs

The tool will create a temporary workspace directory and deploy the lab, run configuration conversion, and write artifacts under the workspace's `output/` directory, e.g. `/tmp/srlconv-1234567890/output`.

| File pattern                                       | Meaning                                                                                |
| -------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `{version}.cfg.json`                               | Current/Target config in JSON format |
| `{version}.cli.txt`           | Current/Target config in CLI format.                                                         |
| `{version}.cli-flat.txt` | Current/Target config in CLI-flat format.                                                    |

## Examples

Minimal conversion:

```bash
srlconv convert \
  --current-version 25.10.1 \
  --current-config ./my-router.cfg.json \
  --target-version 25.10.2
```

Different hardware type on the target and print `git diff` suggestions:

```bash
srlconv convert \
  --current-version v25.10.1 \
  --current-config ./config.json \
  --current-type ixr-d2l \
  --target-version 25.10.2 \
  --target-type ixr-d3l
```
