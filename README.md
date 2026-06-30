# Terraform Provider Schema Archive

This repo contains an archive of the provider schemas for all providers in OpenTofu's registry. The registry is kept immutable.

Structure:

```
archive.json                    # Metadata about which registries are tracked and how they have progressed
maintain-archive/               # The function that maintains archive.json & the archive
  run.py                        # The maintenance engine
  registry.py                   # The registry interface
  registry_opentofu.py          # The driver for the OpenTofu registry
  dump.py                       # Sandboxed schema dumping in Docker
  duration.py                   # Parser for --timeout durations
  Dockerfile                    # The image used to download providers & dump schemas
  pyproject.toml                # Python project & dependencies (managed with uv)
  uv.lock                       # Pinned dependency lockfile
schema-archive/                 # The full archive
  <registry>/                   # The registry of the provider
    <prefix>/                   # The org's first two characters, sharding orgs so no folder grows unbounded
      <org>/                    # The org of the provider
        <provider>/             # The name of the provider
          <version>/            # The version of the provider
            schema.json.gz      # The provider's schema (gzip-compressed), indexed out of `tofu providers schema -json`
            stdout.txt          # The stdout of the schema dump
            stderr.txt          # The stderr of the schema dump
            metadata.json       # Metadata about how the schema was generated
schema-latest/                  # A simplified view of schema-archive, containing only the latest providers
  <registry>/                   # The registry of the provider
    <prefix>/                   # The org's first two characters (same sharding as schema-archive)
      <org>/                    # The org of the provider
        <provider>              # A symlink into schema-archive's tree for the highest version of the published provider
.github/workflows/maintain.yaml # The workflow that maintains the archive via a chron
```

For each invocation: `maintain-archive/run.py` reads `archive.json` for the list of Terraform registries tracked to determine the list of versions that have yet to be processed.


`archive.json` follows:

```json
{
  "crawl-state": {
    "<registry>": <registry-defined-state>
  },
  "status": [
    {
      "registry": "<registry>",
      "org": "<org>",
      "name": "<name>",
      "versions": [
        {
          "version": "<version>",
          "status": "pending" | "done" | "retry" | "failure" | "rejected"
        }
      ]
    }
  ]
}
```

Because the Terraform provider registry API doesn't define an API for listing all providers, each registry needs it's own adapter to populate the list of providers. `maintain-archive/run.py` is responsible for:

1. Driving each registry engine to populate the crawl status.
2. Dumping the schemas of uncrawled providers.

When a provider is crawled, it's launched in a docker container that templates out a trivial Terraform program that depends on the provider, then invokes it to download and run the provider and dump it's JSON schema. This ensures that a malicious provider will not infect the host computer.

For each provider, in addition to the schema, we write `metadata.json`:

```json
{
  "timestamp": "<ISO 8601 timestamp>",
  "status": "success" | "retry" | "failure" | "rejected",
  "format_version": "<schema format version>"
}
```

`format_version` is the `format_version` reported by `tofu providers schema -json`, lifted out of the schema so `schema.json.gz` holds only the provider's schema. It is present only on success.

We write `stdout.txt` & `stderr.txt` for all attempts, regardless of success or failure. "retry" means a transient failure. "failure" implies a perminent failure. "rejected" means the dump succeeded but its schema was too large to archive.

Schemas are stored gzip-compressed as `schema.json.gz` (decompress with `gunzip`/`zcat`). GitHub blocks any push containing a file larger than 100 MiB, so a schema whose compressed size would exceed 80 MiB is "rejected" and never written to disk.

## Orchestration and ordering

A run proceeds in two steps: discover providers, then dump schemas (updating the
`schema-latest/` view as each dump succeeds).

Discovery drives every registry adapter. Each adapter calls back for every provider
version it finds. The engine records previously unseen versions as `pending` and
never downgrades a version that already carries a terminal status, so discovery is
idempotent.

The work queue is every version whose status is `pending` or `retry`. It is ordered
so that the **latest released version of each provider is dumped first**: the engine
randomly samples among providers whose latest released version is still undumped. Only
once every provider's latest released version has been sampled does it back-fill older
versions. Back-fill is **latest-first within each provider**, but the provider worked on
at each step is chosen at random, so newer versions are archived before older ones
without hammering a single provider's releases back-to-back.

"Latest released" copies OpenTofu's default version selection: a released version
always outranks a pre-release (a version with a `-` segment, like `v4.5.0-beta.17`), so
`v4.4.0` is treated as newer than `v4.5.0-beta.17`. A provider whose only versions are
pre-releases falls back to its highest pre-release. The same rule chooses the
`schema-latest/` symlink target.

`--jobs <n>` (`-j`, default 1) runs up to `n` dumps in parallel. Workers pull from the
shared queue in the order above, so the latest-released-first / random-provider
semantics are preserved; the shared archive, checkpoint, symlinks, and `--max` counter
are guarded by a lock. Each in-flight dump occupies its own live line, and the cursor
rests at the start of the line below the live region.

A run stops at the first of these to occur:

- `--timeout <duration>` elapses (e.g. `1h30m`, `2m15s`).
- `--max <n>` provider versions have been dumped.
- The work queue drains.
- Too many consecutive retryable errors occur (the run pauses so the registry is not
  hammered).

Both `--timeout` and `--max` may be passed; the first to trigger wins. With neither
flag the run continues until the queue drains.

Interrupts are honored between and during dumps:

- One `Ctrl-C` lets the in-flight dumps finish and checkpoint, then exits.
- A second `Ctrl-C` aborts all in-flight dumps immediately.

Repeated retryable (e.g. rate-limit) errors trigger exponential backoff so the
registry's download limits are respected.

`archive.json` is rewritten after every dump, so an interrupted or timed-out run never
loses progress and the next run resumes from where it left off. A provider's
`schema-latest/` symlink is updated as soon as a dump succeeds, pointing at the highest
version of that provider that has been dumped successfully. Because the latest version
is dumped first, the symlink tracks the newest working schema immediately; later
back-filled older versions never downgrade it.

## Querying the archive

To query the latest version of every provider, walk `schema-latest/` with
`followlinks=True` (its entries are symlinks into `schema-archive/`) and decompress each
`schema.json.gz`. The decompressed root is a single provider schema:

```json
{
  "provider":            { ... },              // provider config block
  "resource_schemas":    { "<type>": {...} },  // keyed by resource type, e.g. azurerm_resource_group
  "data_source_schemas": { "<type>": {...} },  // keyed by data-source type
  "functions":           { "<name>": {...} }   // keyed by bare function name, e.g. parse_resource_id
}
```

Note that resource and data-source keys are provider-prefixed (`azurerm_...`) while
function keys are bare (`parse_resource_id`).

As a worked example, this finds provider functions whose name clashes with a resource or
data source in the same provider — i.e. a function `bar` in a provider prefixed `foo_`
that also defines `foo_bar`:

```python
#!/usr/bin/env python3
"""Find provider functions whose name clashes with a resource/data-source
name (provider prefix stripped) within the same provider."""
import gzip, json, os

ROOT = "schema-latest"

def strip_prefix(name):
    """Drop the leading '<provider>_' segment: azurerm_foo_bar -> foo_bar."""
    i = name.find("_")
    return name[i + 1:] if i != -1 else name

for dirpath, _, filenames in os.walk(ROOT, followlinks=True):
    if "schema.json.gz" not in filenames:
        continue
    with gzip.open(os.path.join(dirpath, "schema.json.gz")) as f:
        schema = json.load(f)

    functions = schema.get("functions") or {}
    if not functions:
        continue

    # bare name -> full name, for resources and data sources
    resources   = {strip_prefix(n): n for n in schema.get("resource_schemas") or {}}
    datasources = {strip_prefix(n): n for n in schema.get("data_source_schemas") or {}}

    provider = os.path.relpath(dirpath, ROOT)   # e.g. registry.opentofu.org/op/opentofu/azurerm
    for fn in functions:
        for kind, table in (("resource", resources), ("data_source", datasources)):
            if fn in table:
                print(f"{provider}\tfunction:{fn}\tclashes with {kind}:{table[fn]}")
```

Most providers define no functions, so skipping those early keeps a full walk fast.
