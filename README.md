# <img src="shark.svg" width="34" height="34" valign="middle" style="display:inline-block;" /> Shark


**Simple. Easy. Fast. Efficient.**

Shark is an independent, statically-compiled, punctuation-free programming language.
Source code reads as ordinary human instructions; the compiler is strictly deterministic
and produces optimised native machine code with no interpreter and no AI in the loop.

```shark
page Home
  show text Welcome
  show button Start to page Game

page Game
  show text Game started
```

That is a complete, valid Shark program. There are no semicolons, no braces, no
parentheses, no arrows, and no quotation marks around text.

The same program in Hebrew and Japanese compiles to the **identical** intermediate
representation, with the identical canonical hash — not by convention, but because the
only thing a locale supplies is a bijection between human words and stable identifiers.
There is a command in this repository that proves it.

---

## Repository layout

| Path | Contents |
| --- | --- |
| [`spec/`](spec/) | The language specification, 17 documents |
| [`registry/`](registry/) | The machine-readable Clause Signature Registry and locale files |
| [`reference/`](reference/) | The reference compiler: front end, and a native x86-64 back end written from scratch |
| [`compose.py`](compose.py) | The `compose` tool: build, run and check Shark programs |
| [`editor/`](editor/) | Editor support: a language server, syntax highlighting, a VS Code extension |
| [`examples/`](examples/) | Example programs, including one program in three languages |
| [`tests/`](tests/) | 35 tests, each checking one normative claim |
| [`attic/`](attic/lapis/) | The earlier **Lapis** prototype, moved aside unchanged |

## Try it

Build a program into a Windows executable and run it:

```bash
python compose.py run examples/tour.shark
```

```
== Home ==
Welcome
  [1] Start
  [0] Quit
> 1

== Game ==
Game started
```

The result is a few kilobytes of native x86-64 code. The machine code and the `.exe`
format are produced by Shark's own back end ([`reference/x64.py`](reference/x64.py)).
No C compiler, assembler or linker is involved, and the program depends only on
`KERNEL32.dll`, which is part of Windows. The Hebrew and Japanese versions of the program
build to byte-identical executables.

The front end tokenises, parses, and lowers Shark to canonical SIR. It needs nothing
beyond Python's standard library.

```bash
python reference/shark_ref.py examples/tour.shark --emit ast
```

```bash
python reference/shark_ref.py --prove-locale-equivalence
```

```
  en (English)  examples/tour.shark
      page Home
        show text Welcome
        show button Start to page Game
      page Game
        show text Game started
      -> b2:8f8027655cfd2e5d0aa25d26f8841ee554a0c9b15ab9077ac8a8e6c924fc9eef

  he (עברית)  examples/tour.he.shark
      עמוד Home
        הצג טקסט Welcome
        הצג כפתור Start לעמוד Game
      -> b2:8f8027655cfd2e5d0aa25d26f8841ee554a0c9b15ab9077ac8a8e6c924fc9eef

  ja (日本語)  examples/tour.ja.shark
      ページ Home
        Welcome という文字を表示
        Start というボタンを表示 のページへ Game
      -> b2:8f8027655cfd2e5d0aa25d26f8841ee554a0c9b15ab9077ac8a8e6c924fc9eef

RESULT: all three canonical hashes are identical.
```

Check that the registry cannot be ambiguous:

```bash
python reference/shark_ref.py --check-registry
```

```
core registry: 53 signatures, 30 particles
  en: 53 bound signatures, R1-R6 hold, LL(k) with k = 7 (longest head 6, longest terminator 2)
  he: 53 bound signatures, R1-R6 hold, LL(k) with k = 6 (longest head 5, longest terminator 1)
  ja: 53 bound signatures, R1-R6 hold, LL(k) with k = 2 (longest head 1, longest terminator 1)
```

Ask why a line parsed the way it did:

```bash
python reference/shark_ref.py --why examples/pages.shark:12
```

```
examples/pages.shark:12
  show button Go to the final level to page Level Two

  head match     matched='show button', at token=0
  greedy slot    slot='label', value=['Go','to','the','final','level'], stopped at='to page'
  particle       matched='to page'
  ref slot       slot='target', resolved='Level Two'
```

Run the tests:

```bash
python tests/run_tests.py
```

## The specification

### Part I — The language

| # | Document | Covers |
| --- | --- | --- |
| 00 | [Overview and design rules](spec/00-overview.md) | The four principles, the five design rules, the Sugar Law |
| 01 | [Lexical structure](spec/01-lexical-structure.md) | Encoding, word segmentation, indentation, notes, text blocks, bidi |
| 02 | [Grammar](spec/02-grammar.md) | Complete EBNF, the greedy-slot rule, operator precedence |
| 03 | [Clause Signature Registry](spec/03-clause-registry.md) | How punctuation-free parsing is made provably unambiguous (R1–R6) |
| 04 | [Multilingual architecture](spec/04-multilingual.md) | Locales, RTL, unsegmented scripts, source translation, application messages |
| 05 | [Types and semantics](spec/05-types-and-semantics.md) | Type system, inference, records, choices, failure, evaluation order |

### Part II — The implementation

| # | Document | Covers |
| --- | --- | --- |
| 06 | [Memory and safety](spec/06-memory-and-safety.md) | The storage ladder, ownership and region inference with zero annotations |
| 07 | [Concurrency and SIMD](spec/07-concurrency-and-simd.md) | Handler-parallel execution, the conflict relation, auto-vectorisation |
| 08 | [Shark IR](spec/08-sir.md) | SIR-D and SIR-C, 41 opcodes, canonical ordering, the canonical hash |
| 09 | [Compiler pipeline](spec/09-compiler-pipeline.md) | 20 phases, incremental compilation, optimisation levels |
| 10 | [Back ends, targets, ABI](spec/10-backends-targets-abi.md) | Cranelift, LLVM, SPIR-V/WGSL, WASM, the C ABI, the runtime |

### Part III — The platform

| # | Document | Covers |
| --- | --- | --- |
| 11 | [Standard library](spec/11-stdlib.md) | UI, events, net, store, draw2d, scene3d, physics, tensor, sys, test |
| 12 | [Toolchain (`compose`)](spec/12-toolchain.md) | CLI, manifest, packages, testing, hot reload, reproducible builds |
| 13 | [Language Server Protocol](spec/13-lsp.md) | Capabilities, semantic tokens, and the Shark extensions |
| 14 | [Diagnostics](spec/14-diagnostics.md) | Error taxonomy, message style rules, fix-its, beginner mode |
| 15 | [Conformance and editions](spec/15-conformance-and-editions.md) | What an implementation must do to call itself Shark |
| 16 | [Implementation roadmap](spec/16-roadmap.md) | Bootstrap order from nothing to self-hosting |

## The one idea worth knowing

Shark does not try to understand English. It matches against a finite, declared table
of **clause signatures**:

```
show button <label: text> [ to page <target: ref> ]
```

A `text` slot is greedy and stops at the first **complete terminator phrase** declared
for it. So `show button Go to Top` gives the label `Go to Top` — a bare `to` is not
`to page`. And `show button Go to page Home` gives the label `Go` and the target `Home`.

Defining a function *is* registering a signature:

```shark
to damage a target by an amount
  set health of target to health of target minus amount
```

```shark
damage Hero by 10
```

Before any source is parsed, the registry is checked against six conditions
([R1–R6](spec/03-clause-registry.md#36-ambiguity-freedom-conditions-n)) that make
ambiguity impossible by construction. If the registry is ambiguous, the *registry* fails
to build. The program is never parsed at all, and the compiler never guesses.

## Two languages, not one

The language a **programmer** writes in and the language a **user** reads are separate
problems, and Shark keeps them separate.

`show text Hi` puts the *word* `Hi` in the program — it is data, like `42`. Translating
the source to Hebrew translates the instruction, not the word. To greet a Hebrew-speaking
user in Hebrew, the program has to say so:

```shark
the message greeting says
  in english Hi
  in hebrew שלום
  in japanese こんにちは

show text value of greeting
```

The compiler checks that every wording is present and that none of them dropped a
placeholder, so a missing translation is a build failure rather than a user-visible one.
[Section 4.12](spec/04-multilingual.md#412-localising-what-the-application-says-n) has
the rules; [`examples/greeting.shark`](examples/greeting.shark) is the same program in
three source languages, all with one canonical hash.

## Status

This repository is a **specification and reference compiler**, not yet a complete one.

- **Front end.** Lexing (including Japanese word segmentation), the registry and its
  admissibility checks, clause parsing, expressions with precedence, locale binding with
  slot and splice reordering, messages, modules (`use` and `share`), instructions you
  declare with `to`, SIR-D emission and the canonical hash.
- **Native back end.** Windows x86-64 console programs. It covers pages and buttons,
  text with values, whole numbers and yes-or-no values (with overflow and
  division-by-zero traps), messages that pick the user's language at run time, modules,
  and declared instructions. Pages are shown as text, with numbered buttons.
- **Not yet.** `if`, loops, lists, text and decimal values, user input, type checking
  beyond what the back end needs, ownership inference, SIR-C, the optimiser, and targets
  other than Windows. [Document 16](spec/16-roadmap.md) sets out the order to build
  them in, and why.

Everything marked *normative* is fixed for Edition 2026; everything marked *informative*
explains the reasoning behind it.
