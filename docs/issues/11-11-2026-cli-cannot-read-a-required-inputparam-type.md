---
title: "The CLI shows a required InputParam's type as <any> and guesses the value type"
status: open
area: core
filed: 2026-11-11
scope: S
verify: test
---

`InputParam[int]("x")` is a frozen dataclass, so the `int` is lost at runtime
([src/adag/cli.py](../../src/adag/cli.py) `_infer_scalar`). `adag run -h` prints `<any>`, and
`--param x=007` becomes `7`.

Fix: an explicit `type=` argument on `InputParam` (kept as a field), used by the CLI. Test:
`--param` of a str param keeps `"007"`.
