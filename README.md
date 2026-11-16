# pastecc

**Paste C, C++, or C# source into your terminal. Get a binary.**

`pastecc` is a zero-dependency Python CLI that takes source code you paste (or pipe, or point at), works out which language it is, finds a suitable compiler on your system, resolves the libraries the code needs, and builds it. If the build fails for a fixable reason, such as a missing `-lm`, it diagnoses the error and retries. If it can't fix the problem itself, it tells you which package to install.

```
$ pastecc -o hello --run
Paste your C / C++ / C# code. Finish with Ctrl-D, or a line containing only ';;'
#include <stdio.h>
#include <math.h>
int main(void) { printf("%.3f\n", sqrt(2.0)); }
;;
[*] Language: C  (confidence 92%)
[+] Toolchain: /usr/bin/gcc
[*] Libraries: -lm
[+] Built /home/you/hello  [gcc -std=c17]
[*] Running /home/you/hello
----------------------------------------
1.414
```

---

## Features

- **Automatic language detection.** Weighted heuristics (headers, `std::`, `using System;`, `Console.WriteLine`, templates, `malloc` casts, and more) distinguish C, C++, and C#. Override with `--lang`.
- **Toolchain discovery**
  - C: `gcc`, `clang`, `cc`, `tcc`, `cl`
  - C++: `g++`, `clang++`, `c++`, `cl`
  - C#: `dotnet`, `csc`, `mcs` (Mono)
- **Library resolution**
  - Maps `#include`s to link flags (`math.h` → `-lm`, `openssl/ssl.h` → `-lssl -lcrypto`, `curl/curl.h` → `-lcurl`, …).
  - Uses `pkg-config` when it's available.
  - Probes the preprocessor to confirm headers actually exist before compiling.
  - Maps C# `using` directives to the assembly references they need.
- **Self-repair on failure**
  - Undefined-symbol linker errors are mapped back to libraries and the build is retried.
  - Missing headers produce a package-manager hint for your system (`apt`, `dnf`, `pacman`, `brew`, `apk`, `zypper`).
- **Standard fallback.** Tries `c17 → gnu11 → c99` or `c++20 → c++17 → c++14` when an error looks standard-related.
- **Flexible input.** Interactive paste, piped stdin (`pbpaste | pastecc`), or `-f file`.
- **Optional hardening.** `--harden` adds stack protector, `_FORTIFY_SOURCE`, PIE, full RELRO, and a non-executable stack.
- **Safe defaults.** Nothing runs unless you pass `--run`, and NuGet packages are never fetched unless you pass `--allow-nuget`.

## Requirements

- Python 3.8+
- At least one compiler for the language you're building:

| Language | Debian/Ubuntu | Fedora/RHEL | macOS |
|---|---|---|---|
| C / C++ | `build-essential` | `gcc gcc-c++` | `xcode-select --install` |
| C# | `dotnet-sdk-8.0` or `mono-mcs` | `dotnet-sdk-8.0` | `brew install --cask dotnet-sdk` |

`pkg-config` is optional but improves library detection.

## Installation

```bash
curl -o ~/.local/bin/pastecc https://raw.githubusercontent.com/<you>/pastecc/main/pastecc.py
chmod +x ~/.local/bin/pastecc
```

Or clone the repository and symlink `pastecc.py` somewhere on your `PATH`.

## Usage

```bash
pastecc                          # interactive paste; finish with Ctrl-D or a ';;' line
pastecc -o mytool                # name the output binary
pbpaste | pastecc --run          # build from the clipboard (macOS) and run it
xclip -o | pastecc               # same on Linux/X11
pastecc -f snippet.cpp           # build from a file
pastecc --lang cpp               # skip detection
pastecc -c clang++ --std c++17   # choose the compiler and standard
pastecc --harden --static        # hardened, statically linked build
pastecc -- -O3 -march=native     # pass extra flags straight to the compiler
```

If you don't pass `-o`, the binary is named `pasted_<lang>_<hash>`, using the first 8 characters of the SHA-1 of the source. Pasting the same code twice gives the same name.

### Options

| Option | Description |
|---|---|
| `-f, --file FILE` | Read source from a file instead of the terminal |
| `-o, --output NAME` | Output binary name |
| `-l, --lang {c,cpp,cs}` | Force the language |
| `-c, --compiler NAME` | Preferred compiler (`gcc`, `clang++`, `dotnet`, `mcs`, …) |
| `--std STD` | Language standard (`c11`, `c++17`, …) |
| `--harden` | Add exploit-mitigation compiler and linker flags |
| `--static` | Static link (C/C++) |
| `-g, --debug` | Debug build (`-g -O0`) |
| `--run` | Run the binary after a successful build |
| `--save-source` | Write the pasted source next to the binary |
| `--allow-nuget` | Let `dotnet` fetch NuGet packages the code references |
| `--rid RID` | .NET runtime identifier for self-contained builds (`linux-x64`, `win-x64`, …) |
| `--dotnet-tfm VER` | .NET target framework version (default `8.0`) |
| `-v, --verbose` | Show exact compiler commands and all warnings |
| `-- FLAGS…` | Extra flags passed through to the compiler |

## How it works

```
read source ─► detect language ─► pick toolchain ─► resolve libraries ─► compile
                                                                           │
                         ┌─────────────── failure ◄────────────────────────┘
                         ▼
              diagnose stderr ── undefined symbol ──► add lib flags, retry once
                         │
                         ├──── missing header ──────► print install hint, stop
                         │
                         └──── standard error ──────► try next -std, retry
```

**C / C++.** The source is written to a temporary directory and compiled with the detected compiler. The tool collects link flags from the built-in header table, `pkg-config`, and retry diagnostics.

**C# with `mcs` / `csc`.** The source is compiled as a single file, with `-r:` references inferred from `using` directives. On Unix with Mono, a small shell launcher is written alongside the `.exe` so the output runs like a native binary.

**C# with `dotnet`.** A minimal `.csproj` is generated (top-level statements and implicit usings are enabled), and `dotnet publish` produces a single-file executable.

## Security notes

`pastecc` is built to handle code you didn't write, so a few defaults are deliberately conservative:

- **Building does not run anything.** The binary only runs if you pass `--run`. Treat code pasted from the internet the way you'd treat any untrusted executable.
- **NuGet is off by default.** NuGet packages can carry MSBuild targets that execute arbitrary code at build time, and auto-resolving packages from namespace names invites typosquatting. Opt in with `--allow-nuget`.
- **Includes can read local files.** `#include "/some/file"` makes the compiler read that file, and its contents can show up in error output. Keep that in mind if you share build logs.
- **Use `--harden`** for binaries you plan to keep around.

For untrusted input, building inside a disposable container or VM is recommended.

## Extending the library map

Library knowledge lives in three tables at the top of `pastecc.py`:

- `HEADER_LIBS`: header → `(link flags, pkg-config name, {package manager: package})`
- `SYMBOL_LIBS`: regex for undefined symbols → link flags (used during retry)
- `CS_REFS` / `CS_NUGET`: C# namespace → assembly reference / NuGet package

Adding support for a new library usually takes a single line in one of these tables.

## Limitations

- Only one source file per build; multi-file pastes aren't split yet.
- Language detection is heuristic. Very short or unusual snippets may need `--lang`.
- Code that is valid C but not valid C++ (and the reverse) depends on detection being correct.
- Windows/MSVC (`cl`) support is basic and less tested than gcc/clang.
- The `dotnet` path requires network access the first time it restores the SDK's packages.

## Roadmap

- [ ] Automatically retry as C++ when a C build fails with C++-looking errors
- [ ] Split multi-file pastes on `// file: name.ext` markers
- [ ] `--sandbox`: build inside a throwaway container
- [ ] Cross-compiling to Windows via MinGW (`x86_64-w64-mingw32-gcc`)
- [ ] Look up missing headers with `apt-file` / `dnf provides` instead of only the static table
- [ ] Move the library tables into a user-editable data file

## License

MIT
