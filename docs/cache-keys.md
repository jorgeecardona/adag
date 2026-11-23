# Why a step recomputed

Call `explain_key(params)` on a node to list every part of its cache key. Run it before and
after a change: the part whose digest differs is the cause.

## Explain one node

```python
--8<-- "docs/examples/explain_key.py"
```

```text
Scale  key=9ba856cd8cc6  format=2
  identity  -        Scale         88caac9df9ad
  code      source   f7e791aa02b5  3c5791a5408e
  field     factor   2             01e6dab02f90
  skipped   workers  Config(4)     -
  param     x        3             412a6e3e02d7
```

Each row is one component: kind, name, value, and a 12-hex digest of the bytes it adds to the
key. A `skipped` row is not in the key and has no digest.

| Kind | What it is |
|---|---|
| `identity` | the step class name, or its `kind` |
| `code` | a digest of the step class source (name `source`), or the text of `code_version` (name `code_version`) |
| `pinned` | the class sets `cache_key`: it replaces identity, code and fields |
| `field` | a step field, by value |
| `file` | a `FileSource` field, by file content |
| `nested_step` | a field that holds another step, by that step's hash |
| `upstream` | an input wired to another node, by that node's key |
| `param` | an input wired to an `InputParam`, by the run's value or the default |
| `deferred` | an input wired to another source, such as a `FileSource` |
| `raw` | an input passed as a literal value |
| `expansion` | a node that builds its sub-graph at run time (a template fan-out, a loop). The text says what is not known yet. It adds nothing to the key |
| `skipped` | not in the key: a `Config` field, `node_label`, or a value the key cannot encode when `strict_content_hash = False` |

The return value is a `KeyExplanation`. `str()` gives the table above, `to_dict()` gives JSON.

- `key` is `None` when a component blocks it, and `blocked_by` names that component. A
  `FileSource` field whose file is missing does this. A missing `FileSource` wired as an input
  does not: it still gets a key, and the run fails with `FileNotFoundError`.
- `key_format` is `adag.hashing.KEY_FORMAT`. A release that changes every key bumps it.
- A step that overrides `content_hash` (`FanOutStep` does) shows one `identity` row: adag cannot
  see its parts.
- A `FanOutStep` with a `template` also shows an `expansion` row, `one branch per item of 'over';
  the number of branches is known at run time`. Each branch has its own key and its own key
  record, written when the branch runs.

## Find what changed

`Pipeline.explain_keys(params)` explains every node, upstream nodes first. Compare two graphs
component by component:

```python
--8<-- "docs/examples/key_diff.py"
```

```text
Scale: field factor changed: 2 -> 3
AddOne: upstream n changed: Scale cc7c4b4e33b2 -> Scale a926f5aa6596
AddOne record has adag_version: True
record equals explain_key: True
```

The first line that is not `upstream` is the cause. Each `upstream` line after it is a
consequence.

## Keep a key through an edit that changes no result

Any edit inside the step class body changes its `code` digest: a docstring, a comment, a blank
line. Set `code_version` on the class to pin the part of the key that comes from the source:

```python
@dataclass(frozen=True)
class Probe(Step[Findings]):
    code_version = "2026-11-23"

    def run(self, domain: str) -> Findings: ...
```

1. Print the current digest: `class_source_hash(Probe).decode()`.
2. Set `code_version` to that text. The key does not change.
3. Edit the class. The key stays the same until you change `code_version`.

Change `code_version` when the edit changes results. `code_version` applies to the class that
sets it. A subclass does not inherit it. `FanOutStep` and `LoopStep` do not hash their own class
source, so `code_version` does nothing on them.

## Key records

After each save adag writes `<cache_dir>/.keys/<key>.json`: the node's `explain_key` output plus
`record_version`, `adag_version` and `saved_at`. The end of the example above reads one back
with `KeyExplanation.from_dict`.

- Size: under 1 KB for a small step. Each value is cut to 80 characters.
- A skipped component keeps its name and loses its value. A `Config` field can hold a client or a
  secret, and the record goes to disk or to a shared store.
- A failed write never fails the run. adag sends a `step.key_record.failed` event instead.
- With `store=`, the record is the store key `.keys/<key>.json`.
- A `FanOutStep` with a template and a `LoopStep` save no result of their own, but they write a
  record after each run, with `"expansion": {"children": [...]}`: the key of each branch or round
  that ran, in order. Each child has its own record or result under that key.
- adag does not read these records. Use them to compare the key a result was saved under with
  a new key.
