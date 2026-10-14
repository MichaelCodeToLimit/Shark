# Shark: instructions for coding agents

Shark 🦈 is a punctuation-free, multilingual programming language.
The same program can be written in English, Hebrew or Japanese, and it compiles to
identical canonical IR and a byte-identical native executable. The normative
specification is in `spec/00-16`. **The spec wins over the code.** If you change
behaviour, update the spec section that defines it.

## Run things

```
python tests/run_tests.py                          # front-end tests (35, all must pass)
python editor/shark_lsp.py --selftest               # language server self-test (11)
python tests/lsp_client.py                         # LSP over the wire (11)
python compose.py build examples/hello.shark        # native Windows exe in target/
python compose.py run examples/lapis/showcase.shark
python compose.py check <file>                     # front + back end, writes nothing
python reference/shark_ref.py <file> --emit ast|sir|hash
python reference/shark_ref.py --prove-locale-equivalence
```

Python 3.8+ standard library only. Never add dependencies.

## Layout

| Path | What it is |
| --- | --- |
| `spec/` | Normative spec. 02 grammar, 03 registry (R1-R6), 04 multilingual, 05 types, 08 SIR, 10 back ends, 14 diagnostics, 15 conformance/status, 16 roadmap |
| `registry/core.signatures` | Every built-in instruction (53 signatures) and the closed particle set |
| `registry/locales/{en,he,ja}.locale` | Words for every signature, particle, operator and type in each language |
| `reference/shark_ref.py` | Front end: lexer, registry binding, parser, SIR, canonical hash, modules, declared instructions |
| `reference/shark_native.py` | Native back end: SIR to x86-64 machine code. Reads **only** SIR |
| `reference/x64.py` | From-scratch x86-64 encoder (`Asm`) and PE32+ writer (`build_pe`, `read_imports`) |
| `compose.py` | The `compose` CLI (build / run / check / version) |
| `editor/` | LSP server (`shark_lsp.py`), TextMate grammar generator, VS Code extension |
| `examples/` | Programs. `examples/lapis/` holds the old Lapis prototype files converted to Shark |
| `attic/lapis/` | The old Lapis toolchain (PowerShell, C, HTML). Not part of Shark |

## Rules you must not break

1. **Determinism.** The same SIR must always give the same exe bytes: no timestamps,
   no paths, no randomness, no dependence on set or hash ordering.
2. **Locale invariance.** `tour.shark`, `tour.he.shark` and `tour.ja.shark` must have the
   same SIR hash (`b2:8f8027…`) and byte-identical exes. Check with `sha256sum`.
   Canonical words (`yes`, `no`, operator names, CIDs) go into SIR, never source words.
3. **Standalone output.** A built program imports only `KERNEL32.dll` (check with
   `x64.read_imports`). No C runtime, assembler, linker, LLVM, Python, JS or HTML in
   the output.
4. **Nothing is silently dropped.** Unsupported constructs raise a coded error that
   names what is missing. Leftover words on a line are `E-0255`.
5. **Registry admissibility.** Every new signature must pass R1-R6 in every locale
   (`BoundRegistry.check_admissibility`). Every locale must bind every CID (L1), and no
   two things may share a phrase (L2; operators vs `yes`/`no`/`nothing` too).
6. **Error codes** follow spec/14 §14.1 (`E-CCNN`, where `CC` is the phase). Codes are
   permanent and never reused. Messages are plain, friendly English with no jargon.
7. Don't push without asking the owner, Michael Davies.

## How the pieces fit

- **Front end.** Pass 1 lexes the file and finds `use`, `share` and `to …`
  declarations (`top_level_lines`, `declare_actions`, `ModuleLoader`). The registry is
  rebuilt with declared instructions, pass 2 parses, and `to_sir` emits canonical SIR
  (datum pool sorted). Module name = file path words (`use app` → `app.shark` next to
  the entry file).
- **SIR shapes.** Items look like `{"op", "args", "body"}`. Args use `{"const text": i}`
  (pool entry is a list of words and `{"splice": name}`), `{"name": i}` (list of words),
  `{"ref": i}` (string), `{"const number": i}` (source spelling), `{"truth": "yes"|"no"}`,
  `{"apply": op, "left", "right"}`, `{"apply": op, "operand"}`.
  - `use` / `share` are `core.decl.use` / `core.decl.share`.
  - A `to` declaration is `core.decl.action` with `args.shape = {"action": "user.show_title",
    "parts": [...]}`. Calls to it are `op: "user.show_title"`.
- **Back end** (`NativeCompiler`).
  - Each module gets its own names; only `share`d names cross a `use`.
  - Pages are routines. Buttons register in a table, and the main loop prints a
    numbered menu and reads a number.
  - Declared instructions are routines with an RBP frame. The caller reserves an
    aligned argument block: text is (pointer, length), values are by value, and names
    are by address. Routines are specialised per argument-type tuple.
  - Text containing values is assembled by redirecting `rt_write` into a 64 KB text
    space (`g_sink`), which is released when the call returns.
  - Messages are inlined at each use and pick their wording from `g_locale` (from
    `SHARK_LOCALE`, otherwise the Windows UI language).
  - Traps exit with the spec/10 §10.8 reason code (1 overflow, 2 divide by zero,
    9 out of text space) and print what failed and where to stderr.
- **Win64 ABI.** Every routine does `sub rsp,40` (or `push rbp; sub rsp,16n+32`) so the
  stack is 16-byte aligned at each call. Arguments go in RCX, RDX, R8, R9, and the 5th
  goes at `[rsp+32]`.

## Current state (2026-09-25)

Working and verified by hand, **but uncommitted**:

- The native back end and compose.
  - These build and run: hello, tour ×3, greeting ×3, pages, ambiguity, and
    lapis/{app, app_page1, registry, showcase}.
  - Not buildable yet: lapis/commands and lapis/charts are libraries (they build
    to an exe that does nothing).
  - brain, game and social use grammar the reference front end doesn't have.
- Front-end additions:
  - `yes`/`no` values.
  - `not` precedence fixed to spec/02 §2.7.1 level 6.
  - `and`/`or` short-circuit natively.
  - Declared instructions (spec/03 §3.4).
  - Modules.
  - The `E-0255` leftover-words check.
  - The L2 check now covers operators.
- Hebrew `not` was changed from `לא` to `לא נכון ש`, because `לא` is also the value
  `no`. **A native Hebrew speaker should review this wording.**

## Tasks, in order

### 1. Lock in what exists

- [ ] Add back-end tests to `tests/run_tests.py`:
  - [ ] Build each buildable example.
  - [ ] Run it with piped input and compare stdout.
  - [ ] tour/tour.he/tour.ja produce byte-identical exes.
  - [ ] Imports are only KERNEL32.
  - [ ] Traps exit 1 and 2.
  - [ ] `SHARK_LOCALE=hebrew` selects the Hebrew wording.
  - [ ] Errors E-0254, E-0255, E-0311, E-0312, E-0315, E-0413, E-0522 fire.
  - [ ] Skip the native tests when not on Windows.
- [ ] Make the "every example compiles" test walk subdirectories (examples/lapis).
- [ ] Wire `editor/shark_lsp.py --selftest` and `tests/lsp_client.py` into run_tests.
  Confirm the LSP copes with the new SIR items (`core.decl.use/share`, `user.*` ops).
- [ ] Docs:
  - [ ] spec/10: the implemented native Windows back end, its console page model and
    its calling convention.
  - [ ] spec/05 §5.6: whole-number `over` rounds toward zero, and `remainder of`
    takes the dividend's sign.
  - [ ] spec/14: the new codes E-0254/0255/0311/0315/0317/0318/0415/0416/0417/0501/
    0502/0503/0522/0523/0524/0571/1001/1002/1003/1201/1202.
  - [ ] spec/15 §15.8 status table and spec/16 roadmap.
  - [ ] README: build and run natively, and the test count.
  - [ ] attic/lapis/README.md: the language files moved to examples/lapis.
- [ ] Commit (ask the owner first).

### 2. More syntax (owner request)

The spec already defines these, but nothing implements them. See spec/02 lines 204-220.

For each construct:

- Add a signature to `registry/core.signatures`.
- Add words in all three locales. The en/he/ja `word if/otherwise/while/repeat/for each/
  stop/skip/give` entries already exist.
- Make it pass R1-R6.
- Parse it in the front end, emit SIR, and generate native code.
- Add tests, including an en/he/ja equivalence test.

Order:

1. `if <condition>` / `otherwise if` / `otherwise` blocks.
2. `repeat <n> times`, then `while <condition>`, `stop`, `skip`.
3. `give <value>` in `to` instructions, so they can return a value and be used
   inside expressions (spec calls these value signatures, `gives <type>`).
4. Asking the user for input. `show field … into <name>` is already in the registry.
   On the console, read a line into a text or whole-number variable.
5. Text variables (`let name be …` holding text), `join`, and `length of`.
6. Lists: `list of`, `add … to`, `remove … from`, `for each x in list`, `count of`.
7. `random number between a and b`, `wait for n seconds`.
8. Decimal numbers (currently `E-1002`).

### 3. Make it easier (owner request)

- [ ] "Did you mean" suggestions on `E-0210` (unknown instruction) and `E-0501`
  (unknown name), by edit distance over registry heads and declared names in the
  file's locale.
- [ ] On a page, let the user type a button's label as well as its number.
- [ ] `compose new <name>` (spec/12): create a folder with a runnable `main.shark`.
- [ ] Run a file with `compose <file>`, without typing `build`.
- [ ] `compose explain <code>`: print the long form of any error (spec/14 §14.4).
- [ ] A step-by-step beginner guide, `docs/learn.md`, where every snippet is a real
  file that tests compile.
- [ ] Declared instructions only work in English right now (the articles a/an/the).
  Add a locale binding for the slot marker so Hebrew and Japanese can declare
  instructions.
- [ ] Keep every new wording natural in all three languages. Add no punctuation to the
  language (spec/00 Rule 1).

### 4. Known gaps

- Cyclic `use` between modules is `E-1202`. The spec allows it for signatures.
- Message wordings should be sorted by locale tag in SIR (spec/04 §4.12.2 rule 6).
  Verify this.
- There is only a Windows x86-64 target. Next would be Linux ELF, written from scratch
  like `x64.py`.
- The compiler itself is Python. Being fully standalone needs a self-hosted compiler
  (spec/16 `shark0`). That's long-term: grow the language (tasks 2-3) until the
  compiler can be written in Shark.
- `editor/README.md` (per-editor setup) is not written. The proposed `.gitattributes`
  (Linguist override) is not added.
