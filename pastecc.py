#!/usr/bin/env python3
"""
pastecc - paste C / C++ / C# source into the terminal, get a binary.

    pastecc                 # interactive: paste code, finish with Ctrl-D (or a line containing only ';;')
    pastecc -o tool         # name the output
    pbpaste | pastecc       # piped input works too
    pastecc -f snippet.cpp  # or a file
    pastecc --run           # run the binary after a successful build (opt-in)

Pipeline: read -> detect language -> find toolchain -> resolve libraries -> compile
          -> on failure, diagnose (missing headers / undefined symbols) and retry once.
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

# ----------------------------------------------------------------------------- output helpers
TTY = sys.stderr.isatty()
def _c(code, s): return f"\033[{code}m{s}\033[0m" if TTY else s
def info(s): print(_c("36", "[*] ") + s, file=sys.stderr)
def ok(s):   print(_c("32", "[+] ") + s, file=sys.stderr)
def warn(s): print(_c("33", "[!] ") + s, file=sys.stderr)
def err(s):  print(_c("31", "[-] ") + s, file=sys.stderr)

# ----------------------------------------------------------------------------- knowledge tables
# header -> (link flags, pkg-config name or None, {distro: package})
HEADER_LIBS = {
    "math.h":            (["-lm"], None, {}),
    "cmath":             (["-lm"], None, {}),
    "pthread.h":         (["-pthread"], None, {}),
    "thread":            (["-pthread"], None, {}),
    "dlfcn.h":           (["-ldl"], None, {}),
    "zlib.h":            (["-lz"], "zlib", {"apt": "zlib1g-dev", "dnf": "zlib-devel", "brew": "zlib"}),
    "openssl/ssl.h":     (["-lssl", "-lcrypto"], "openssl", {"apt": "libssl-dev", "dnf": "openssl-devel", "brew": "openssl"}),
    "openssl/evp.h":     (["-lcrypto"], "libcrypto", {"apt": "libssl-dev", "dnf": "openssl-devel", "brew": "openssl"}),
    "openssl/sha.h":     (["-lcrypto"], "libcrypto", {"apt": "libssl-dev", "dnf": "openssl-devel", "brew": "openssl"}),
    "curl/curl.h":       (["-lcurl"], "libcurl", {"apt": "libcurl4-openssl-dev", "dnf": "libcurl-devel", "brew": "curl"}),
    "sqlite3.h":         (["-lsqlite3"], "sqlite3", {"apt": "libsqlite3-dev", "dnf": "sqlite-devel", "brew": "sqlite"}),
    "ncurses.h":         (["-lncurses"], "ncurses", {"apt": "libncurses-dev", "dnf": "ncurses-devel", "brew": "ncurses"}),
    "curses.h":          (["-lncurses"], "ncurses", {"apt": "libncurses-dev", "dnf": "ncurses-devel", "brew": "ncurses"}),
    "readline/readline.h": (["-lreadline"], "readline", {"apt": "libreadline-dev", "dnf": "readline-devel", "brew": "readline"}),
    "pcap.h":            (["-lpcap"], "libpcap", {"apt": "libpcap-dev", "dnf": "libpcap-devel", "brew": "libpcap"}),
    "pcap/pcap.h":       (["-lpcap"], "libpcap", {"apt": "libpcap-dev", "dnf": "libpcap-devel", "brew": "libpcap"}),
    "SDL2/SDL.h":        (["-lSDL2"], "sdl2", {"apt": "libsdl2-dev", "dnf": "SDL2-devel", "brew": "sdl2"}),
    "GL/gl.h":           (["-lGL"], "gl", {"apt": "libgl-dev", "dnf": "mesa-libGL-devel"}),
    "GLFW/glfw3.h":      (["-lglfw"], "glfw3", {"apt": "libglfw3-dev", "dnf": "glfw-devel", "brew": "glfw"}),
    "uuid/uuid.h":       (["-luuid"], "uuid", {"apt": "uuid-dev", "dnf": "libuuid-devel"}),
    "sodium.h":          (["-lsodium"], "libsodium", {"apt": "libsodium-dev", "dnf": "libsodium-devel", "brew": "libsodium"}),
    "json-c/json.h":     (["-ljson-c"], "json-c", {"apt": "libjson-c-dev", "dnf": "json-c-devel", "brew": "json-c"}),
    "nlohmann/json.hpp": ([], None, {"apt": "nlohmann-json3-dev", "dnf": "json-devel", "brew": "nlohmann-json"}),
    "boost/asio.hpp":    (["-pthread"], None, {"apt": "libboost-dev", "dnf": "boost-devel", "brew": "boost"}),
    "boost/filesystem.hpp": (["-lboost_filesystem"], None, {"apt": "libboost-filesystem-dev", "dnf": "boost-devel", "brew": "boost"}),
    "winsock2.h":        (["-lws2_32"], None, {}),   # mingw / Windows only
    "windows.h":         ([], None, {}),
}

# undefined-symbol prefix -> flags (used when the linker complains and headers didn't tell us)
SYMBOL_LIBS = [
    (r"^(sqrt|cbrt|pow|hypot|a?sinh?|a?cosh?|a?tanh?|atan2|floor|ceil|trunc|log(10|2|1p)?|exp(2|m1)?|fabs|fmod|l?l?round|rint|erfc?|tgamma|lgamma|fmin|fmax|copysign)[fl]?$", ["-lm"]),
    (r"^pthread_", ["-pthread"]),
    (r"^(dlopen|dlsym|dlclose|dlerror)$", ["-ldl"]),
    (r"^(SSL_|TLS_)", ["-lssl", "-lcrypto"]),
    (r"^(EVP_|SHA\d|MD5|RAND_|BIO_|ERR_)", ["-lcrypto"]),
    (r"^curl_", ["-lcurl"]),
    (r"^sqlite3_", ["-lsqlite3"]),
    (r"^(deflate|inflate|compress|uncompress|crc32|adler32)", ["-lz"]),
    (r"^pcap_", ["-lpcap"]),
    (r"^SDL_", ["-lSDL2"]),
    (r"^(initscr|endwin|printw|mvprintw|wrefresh|refresh|getch|noecho|cbreak)$", ["-lncurses"]),
    (r"^std::filesystem::", ["-lstdc++fs"]),
]

# C# namespace -> mcs -r: reference (BCL assemblies not referenced by default)
CS_REFS = {
    "System.Net.Http": "System.Net.Http",
    "System.Numerics": "System.Numerics",
    "System.Drawing": "System.Drawing",
    "System.Windows.Forms": "System.Windows.Forms",
    "System.Data": "System.Data",
    "System.Xml.Linq": "System.Xml.Linq",
    "System.Web": "System.Web",
    "System.Runtime.Serialization": "System.Runtime.Serialization",
    "System.IO.Compression": "System.IO.Compression",
    "System.Management": "System.Management",
}
# Namespaces that live in NuGet packages, not the BCL. Never auto-installed (see --allow-nuget).
CS_NUGET = {
    "Newtonsoft.Json": "Newtonsoft.Json",
    "Spectre.Console": "Spectre.Console",
    "Dapper": "Dapper",
    "Serilog": "Serilog",
    "CommandLine": "CommandLineParser",
}

# ----------------------------------------------------------------------------- input
def read_source(args):
    if args.file:
        with open(args.file, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    if not sys.stdin.isatty():
        return re.split(r"^\s*;;\s*$", sys.stdin.read(), maxsplit=1, flags=re.M)[0]
    print(_c("1", "Paste your C / C++ / C# code. Finish with Ctrl-D, or a line containing only ';;'"),
          file=sys.stderr)
    lines = []
    try:
        for line in sys.stdin:
            if line.strip() == ";;":
                break
            lines.append(line)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        sys.exit(130)
    # strip bracketed-paste markers some terminals leak through
    return "".join(lines).replace("\x1b[200~", "").replace("\x1b[201~", "")

def strip_comments_and_strings(src):
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    src = re.sub(r'@?"(?:\\.|[^"\\\n])*"', '""', src)
    return src

# ----------------------------------------------------------------------------- detection
def detect_language(src):
    code = strip_comments_and_strings(src)
    includes = re.findall(r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]", src, re.M)
    s = {"c": 0, "cpp": 0, "cs": 0}

    # C#
    if re.search(r"^\s*using\s+System(\.[\w.]+)?\s*;", code, re.M): s["cs"] += 6
    if re.search(r"\bstatic\s+(async\s+)?(void|int|Task(<int>)?)\s+Main\s*\(", code): s["cs"] += 5
    if re.search(r"\bConsole\.(Write|WriteLine|ReadLine|ReadKey)\b", code): s["cs"] += 5
    if re.search(r"^\s*namespace\s+[\w.]+\s*;", code, re.M): s["cs"] += 4   # file-scoped namespace
    if re.search(r"\b(public|private|internal)\s+(sealed\s+|static\s+)?(class|record|struct)\b", code): s["cs"] += 1
    if re.search(r"\b(string\[\]\s+args|\bvar\s+\w+\s*=\s*new\b|\bforeach\s*\(\s*var\b|=>\s*\{?)", code): s["cs"] += 1
    if re.search(r"\{\s*get\s*;\s*(set\s*;)?\s*\}", code): s["cs"] += 3

    # C++
    cpp_headers = {"iostream", "vector", "string", "map", "unordered_map", "memory", "algorithm",
                   "thread", "mutex", "fstream", "sstream", "set", "array", "optional", "chrono",
                   "functional", "filesystem", "cmath", "cstdio", "cstdlib", "cstring", "iomanip",
                   "regex", "random", "queue", "deque", "list", "tuple", "variant", "span", "ranges",
                   "format", "print", "atomic", "future", "numeric", "utility"}
    for h in includes:
        if h in cpp_headers or h.endswith((".hpp", ".hh", ".hxx")) or h.startswith("boost/"):
            s["cpp"] += 4
    if re.search(r"\bstd::", code): s["cpp"] += 5
    if re.search(r"\b(cout|cin|cerr|endl)\b", code): s["cpp"] += 3
    if re.search(r"^\s*using\s+namespace\s+\w+", code, re.M): s["cpp"] += 4
    if re.search(r"\btemplate\s*<", code): s["cpp"] += 4
    if re.search(r"\b(class|struct)\s+\w+\s*(:\s*(public|private|protected)\s+\w+)?\s*\{", code) and includes:
        s["cpp"] += 1
    if re.search(r"\b(nullptr|constexpr|static_cast|reinterpret_cast|dynamic_cast|noexcept)\b", code): s["cpp"] += 4
    if re.search(r"\bnew\s+\w+(\[|\()", code) and includes: s["cpp"] += 1
    if re.search(r"\w::\w", code) and includes: s["cpp"] += 1

    # C
    c_headers = {"stdio.h", "stdlib.h", "string.h", "unistd.h", "stdint.h", "stdbool.h", "math.h",
                 "sys/types.h", "sys/socket.h", "netinet/in.h", "arpa/inet.h", "fcntl.h", "errno.h",
                 "signal.h", "time.h", "ctype.h", "pthread.h", "windows.h", "winsock2.h"}
    for h in includes:
        if h in c_headers: s["c"] += 2
    if re.search(r"\b(printf|scanf|malloc|free|fprintf|puts)\s*\(", code): s["c"] += 2
    if re.search(r"\bint\s+main\s*\(", code): s["c"] += 1; s["cpp"] += 1
    if re.search(r"\b(typedef\s+struct|struct\s+\w+\s+\w+\s*[;=])", code): s["c"] += 1
    # C-only constructs that are invalid C++: implicit void* conversion from malloc, designated init of arrays...
    if re.search(r"\w+\s*\*\s*\w+\s*=\s*malloc\s*\(", code): s["c"] += 2
    if re.search(r"\b(class|template|namespace|new|delete|this)\b", code): s["c"] -= 1

    lang = max(s, key=s.get)
    total = sum(max(v, 0) for v in s.values()) or 1
    return lang, s, round(max(s[lang], 0) / total, 2), includes

# ----------------------------------------------------------------------------- toolchains
def find_tool(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None

def pick_toolchain(lang, prefer=None):
    if lang == "c":
        order = [prefer] if prefer else []
        return find_tool(*(order + ["gcc", "clang", "cc", "tcc", "cl"]))
    if lang == "cpp":
        order = [prefer] if prefer else []
        return find_tool(*(order + ["g++", "clang++", "c++", "cl"]))
    if lang == "cs":
        return find_tool(*([prefer] if prefer else []) + ["dotnet", "csc", "mcs"])
    return None

def pkg_manager():
    for pm in ("apt", "dnf", "pacman", "brew", "apk", "zypper"):
        if shutil.which(pm if pm != "apt" else "apt-get"):
            return pm
    return None

def header_available(compiler, lang, header, extra):
    probe = f"#include <{header}>\n"
    x = "c" if lang == "c" else "c++"
    r = subprocess.run([compiler, "-x", x, "-E", "-", "-o", os.devnull] + extra,
                       input=probe, text=True, capture_output=True)
    return r.returncode == 0

def pkgconfig(name, what):
    if not name or not shutil.which("pkg-config"):
        return None
    r = subprocess.run(["pkg-config", what, name], capture_output=True, text=True)
    return r.stdout.split() if r.returncode == 0 else None

# ----------------------------------------------------------------------------- C / C++ build
def resolve_native_deps(compiler, lang, includes):
    cflags, ldflags, missing = [], [], []
    for h in includes:
        entry = HEADER_LIBS.get(h)
        if not entry:
            continue
        flags, pc, pkgs = entry
        pc_cflags, pc_libs = pkgconfig(pc, "--cflags"), pkgconfig(pc, "--libs")
        if pc_cflags: cflags += pc_cflags
        if not header_available(compiler, lang, h, cflags):
            missing.append((h, pkgs))
            continue
        ldflags += pc_libs if pc_libs else flags
    # de-dupe, keep order
    dedupe = lambda xs: list(dict.fromkeys(xs))
    return dedupe(cflags), dedupe(ldflags), missing

def native_command(compiler, lang, src_path, out, cflags, ldflags, std, args):
    name = os.path.basename(compiler)
    if name == "cl":  # MSVC
        cmd = [compiler, "/nologo", "/EHsc", "/O2", f"/Fe:{out}", src_path]
        if std and lang == "cpp": cmd.insert(1, f"/std:{std.replace('c++', 'c++')}")
        return cmd
    cmd = [compiler]
    if std: cmd.append(f"-std={std}")
    cmd += ["-O2", "-Wall"]
    if args.debug: cmd += ["-g", "-O0"]
    if args.harden:
        cmd += ["-fstack-protector-strong", "-D_FORTIFY_SOURCE=2", "-fPIE", "-pie",
                "-Wl,-z,relro,-z,now", "-Wl,-z,noexecstack"]
    if args.static: cmd.append("-static")
    cmd += cflags + [src_path, "-o", out] + ldflags + args.extra
    return cmd

def run(cmd, verbose):
    if verbose:
        info("$ " + " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)

def diagnose_native(stderr):
    """Return (extra_link_flags, missing_headers) inferred from compiler/linker output."""
    flags, headers = [], []
    for sym in re.findall(r"undefined reference to [`'\"]([^'`\"(]+)", stderr):
        for pat, f in SYMBOL_LIBS:
            if re.search(pat, sym):
                flags += f
                break
    for sym in re.findall(r"Undefined symbols.*?\n\s+\"_?([^\"]+)\"", stderr, re.S):  # macOS ld
        for pat, f in SYMBOL_LIBS:
            if re.search(pat, sym):
                flags += f
    headers += re.findall(r"fatal error: ([^:\s]+): No such file", stderr)
    headers += re.findall(r"fatal error: '([^']+)' file not found", stderr)
    return list(dict.fromkeys(flags)), headers

def build_native(lang, src, args):
    compiler = pick_toolchain(lang, args.compiler)
    if not compiler:
        err(f"No {'C' if lang == 'c' else 'C++'} compiler found (looked for gcc/clang/cc/cl).")
        hint_install({"apt": "build-essential", "dnf": "gcc gcc-c++", "brew": "(xcode-select --install)",
                      "pacman": "base-devel", "apk": "build-base"})
        return None
    ok(f"Toolchain: {compiler}")

    work = tempfile.mkdtemp(prefix="pastecc-")
    src_path = os.path.join(work, "main.c" if lang == "c" else "main.cpp")
    with open(src_path, "w") as fh:
        fh.write(src)
    out = os.path.abspath(args.output)

    includes = re.findall(r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]", src, re.M)
    cflags, ldflags, missing = resolve_native_deps(compiler, lang, includes)
    if ldflags: info(f"Libraries: {' '.join(ldflags)}")
    if missing:
        for h, pkgs in missing:
            err(f"Header <{h}> not found on this system.")
            hint_install(pkgs)
        return None

    stds = ([args.std] if args.std else
            ["c17", "gnu11", "c99"] if lang == "c" else ["c++20", "c++17", "c++14"])
    last = None
    for std in stds:
        for attempt in range(2):
            r = run(native_command(compiler, lang, src_path, out, cflags, ldflags, std, args), args.verbose)
            last = r
            if r.returncode == 0:
                if r.stderr.strip() and args.verbose:
                    warn("Compiler warnings:\n" + r.stderr.rstrip())
                elif r.stderr.strip():
                    warn(f"{r.stderr.count('warning:')} warning(s) (use -v to see them)")
                ok(f"Built {out}  [{os.path.basename(compiler)} -std={std}]")
                return out
            extra, hdrs = diagnose_native(r.stderr)
            new = [f for f in extra if f not in ldflags]
            if hdrs:
                for h in hdrs:
                    err(f"Header <{h}> not found.")
                    hint_install(HEADER_LIBS.get(h, (None, None, {}))[2])
                return fail(r)
            if new and attempt == 0:
                warn(f"Linker missing symbols, retrying with {' '.join(new)}")
                ldflags += new
                continue
            break
        # If the error looks standard-related, try the next standard; otherwise stop.
        if not re.search(r"(requires|only available|not supported).*(-std|C\+\+\d\d)|was not declared|is not a member of 'std'",
                         last.stderr):
            break
    return fail(last)

# ----------------------------------------------------------------------------- C# build
def build_csharp(src, args):
    tool = pick_toolchain("cs", args.compiler)
    if not tool:
        err("No C# toolchain found (looked for dotnet, csc, mcs).")
        hint_install({"apt": "dotnet-sdk-8.0  (or mono-mcs)", "dnf": "dotnet-sdk-8.0",
                      "brew": "--cask dotnet-sdk", "pacman": "dotnet-sdk"})
        return None
    ok(f"Toolchain: {tool}")
    usings = re.findall(r"^\s*using\s+(?:static\s+)?([\w.]+)\s*;", strip_comments_and_strings(src), re.M)
    nuget = sorted({pkg for ns in usings for pfx, pkg in CS_NUGET.items() if ns == pfx or ns.startswith(pfx + ".")})
    out = os.path.abspath(args.output)
    work = tempfile.mkdtemp(prefix="pastecc-")

    if os.path.basename(tool) == "dotnet":
        return build_dotnet(tool, src, nuget, out, work, args)

    # mcs / csc: single-file compile
    if nuget:
        err(f"Code needs NuGet package(s): {', '.join(nuget)} - mcs/csc can't fetch these. Install the .NET SDK.")
        return None
    refs = sorted({CS_REFS[ns] for ns in usings if ns in CS_REFS})
    if refs: info(f"References: {', '.join(refs)}")
    src_path = os.path.join(work, "Program.cs")
    with open(src_path, "w") as fh:
        fh.write(src)
    exe = out if out.endswith(".exe") else out + ".exe"
    cmd = [tool, "-optimize+", f"-out:{exe}"] + [f"-r:{r}" for r in refs]
    if os.path.basename(tool) == "mcs": cmd.append("-langversion:experimental")
    cmd += args.extra + [src_path]
    r = run(cmd, args.verbose)
    if r.returncode != 0:
        return fail(r)
    # Give the user something directly executable on Unix
    if os.name != "nt" and shutil.which("mono"):
        with open(out, "w") as fh:
            fh.write(f'#!/bin/sh\nexec mono "{exe}" "$@"\n')
        os.chmod(out, 0o755)
        ok(f"Built {exe} (+ launcher {out})")
        return out
    ok(f"Built {exe}")
    return exe

def build_dotnet(dotnet, src, nuget, out, work, args):
    if nuget and not args.allow_nuget:
        err(f"Code references NuGet package(s): {', '.join(nuget)}.")
        warn("Packages can run arbitrary MSBuild targets at build time. Re-run with --allow-nuget to fetch them.")
        return None
    name = re.sub(r"\W", "_", os.path.basename(out)) or "app"
    pkg_refs = "".join(f'    <PackageReference Include="{p}" Version="*" />\n' for p in nuget)
    rid = args.rid or ""
    csproj = f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net{args.dotnet_tfm}</TargetFramework>
    <ImplicitUsings>enable</ImplicitUsings>
    <Nullable>disable</Nullable>
    <AssemblyName>{name}</AssemblyName>
    <AllowUnsafeBlocks>true</AllowUnsafeBlocks>
    <InvariantGlobalization>true</InvariantGlobalization>
  </PropertyGroup>
  <ItemGroup>
{pkg_refs}  </ItemGroup>
</Project>
"""
    with open(os.path.join(work, f"{name}.csproj"), "w") as fh: fh.write(csproj)
    with open(os.path.join(work, "Program.cs"), "w") as fh: fh.write(src)
    pub = os.path.join(work, "publish")
    cmd = [dotnet, "publish", work, "-c", "Release", "-o", pub, "-p:PublishSingleFile=true",
           "--self-contained", "true" if rid else "false", "--nologo"]
    if rid: cmd += ["-r", rid]
    r = run(cmd, args.verbose)
    if r.returncode != 0:
        return fail(r)
    built = os.path.join(pub, name + (".exe" if os.name == "nt" or rid.startswith("win") else ""))
    shutil.copy2(built, out)
    ok(f"Built {out}  [dotnet {args.dotnet_tfm}]")
    return out

# ----------------------------------------------------------------------------- shared
def hint_install(pkgs):
    pm = pkg_manager()
    if not pkgs:
        return
    if pm and pm in pkgs:
        verb = {"apt": "sudo apt-get install", "dnf": "sudo dnf install", "pacman": "sudo pacman -S",
                "brew": "brew install", "apk": "sudo apk add", "zypper": "sudo zypper install"}[pm]
        warn(f"Try: {verb} {pkgs[pm]}")
    else:
        warn("Packages: " + ", ".join(f"{k}: {v}" for k, v in pkgs.items()))

def fail(r):
    err("Build failed:")
    lines = (r.stderr or r.stdout).rstrip().splitlines()
    print("\n".join(lines[:40]) + ("\n   ..." if len(lines) > 40 else ""), file=sys.stderr)
    return None

def main():
    ap = argparse.ArgumentParser(description="Paste C/C++/C# source, get a binary.")
    ap.add_argument("-f", "--file", help="read source from a file instead of the terminal")
    ap.add_argument("-o", "--output", help="output binary name (default: derived from code hash)")
    ap.add_argument("-l", "--lang", choices=["c", "cpp", "cs"], help="skip detection")
    ap.add_argument("-c", "--compiler", help="preferred compiler (gcc, clang++, dotnet, mcs, ...)")
    ap.add_argument("--std", help="language standard, e.g. c11, c++17")
    ap.add_argument("--harden", action="store_true", help="add stack protector / FORTIFY / PIE / RELRO")
    ap.add_argument("--static", action="store_true", help="static link (C/C++)")
    ap.add_argument("-g", "--debug", action="store_true", help="debug build")
    ap.add_argument("--allow-nuget", action="store_true", help="let dotnet fetch NuGet packages the code needs")
    ap.add_argument("--rid", default="", help="dotnet runtime id for self-contained builds (linux-x64, win-x64...)")
    ap.add_argument("--dotnet-tfm", default="8.0", help="dotnet target framework version (default 8.0)")
    ap.add_argument("--run", action="store_true", help="run the binary after building")
    ap.add_argument("--save-source", action="store_true", help="also write the pasted source next to the binary")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("extra", nargs="*", help="extra flags passed to the compiler (after --)")
    args = ap.parse_args()

    src = read_source(args)
    if not src.strip():
        err("No source received.")
        return 1

    lang, scores, conf, _ = (args.lang, {}, 1.0, None) if args.lang else detect_language(src)
    label = {"c": "C", "cpp": "C++", "cs": "C#"}[lang]
    if args.lang:
        info(f"Language: {label} (forced)")
    else:
        info(f"Language: {label}  (confidence {conf:.0%}; scores {scores})")
        if conf < 0.5:
            warn("Low confidence - use --lang to override if this is wrong.")

    digest = hashlib.sha1(src.encode()).hexdigest()[:8]
    args.output = args.output or f"pasted_{lang}_{digest}"

    if args.save_source:
        ext = {"c": ".c", "cpp": ".cpp", "cs": ".cs"}[lang]
        with open(args.output + ext, "w") as fh: fh.write(src)
        info(f"Source saved to {args.output + ext}")

    built = build_csharp(src, args) if lang == "cs" else build_native(lang, src, args)
    if not built:
        return 1
    if args.run:
        info(f"Running {built}\n" + "-" * 40)
        return subprocess.call([built])
    return 0

if __name__ == "__main__":
    sys.exit(main())
