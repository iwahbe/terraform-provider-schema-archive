# Terraform Provider Schema Archive

This repo contains an archive of the provider schemas for all providers in OpenTofu's registry. The registry is kept immutable.

Structure:

```
archive.json                    # Metadata about which registries are tracked and how they have progressed
maintain-archive/               # The function that maintains archive.json & the archive
  maintain.py                   # The maintence engine
  registry-opentofu.py          # The driver for the OpenTofu registry
schema-archive/                 # The full archive
  <registry>/                   # The registry of the provider
    <org>/                      # The org of the provider
      <provider>/               # The name of the provider
        <version>/              # The version of the provider
          schema.json           # The schema, as dumped by `terraform providers schema -json`
          stdout.txt            # The stdout of the schema dump
          stderr.txt            # The stderr of the schema dump
          metadata.json         # Metadata about how the schema was generated
schema-latest/                  # A simplified view of schema-archive, containing only the latest providers
  <registry>/                   # The registry of the provider
    <org>/                      # The org of the provider
      <provider>                # A symlink into schema-archive's tree for the highest version of the published provider
.github/workflows/maintain.yaml # The workflow that maintains the archive via a chron
```

For each invocation: `maintain-archive.py` reads `archive.json` for the list of Terraform Regestries tracked to determine the list of versions that have yet to be processed.


`archive.json` follows:

```json
{
  "crawl-state": {
    "<registry>": <registry-defined-state>,
  }
  "status": [
    "registry": "<registry>",
    "org": "<org>",
    "name": "<name>",
    "versions": [
      { 
        "version": "<version>", 
        "status": "pending" | "done" | "retry" | "failure"
      },
    ]
  ],
}
```

Because the Terraform provider registry API doesn't define an API for listing all providers, each registry needs it's own adapter to populate the list of providers. `maintain-archive/maintain.py` is responsible for:

1. Driving each registry engine to populate the crawl status.
2. Dumping the schemas of uncrawled providers.

When a provider is crawled, it's launched in a docker container that templates out a trivial Terraform program that depends on the provider, then invokes it to download and run the provider and dump it's JSON schema. This ensures that a malicious provider will not infect the host computer.

For each provider, in addition to the schema, we write `metadata.json`:

```json
{
  "timestamp": "<ISO 8601 timestamp>",
  "status": "success" | "retry" | "failure"
}
```

We write `stdout.txt` & `stderr.txt` for all attempts, regardless of success or failure. "retry" means a transient failure. "failure" implies a perminent failure.
