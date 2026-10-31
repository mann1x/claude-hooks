//! council-sandbox — runs one command in a jail (plan E2, design 04 §5.3).
//!
//! It is the native replacement for the P1 chain
//! `prlimit → setpriv → bwrap`: one static binary, no dependencies, so it
//! runs on any Linux of the right architecture regardless of its libc
//! (the fork's launcher needed glibc 2.34 and never started on bullseye).
//! The cgroup layer stays outside, in `systemd-run --scope`.
//!
//! The command line is the subset of bwrap's vocabulary the engine emits
//! (`claude_hooks/sandbox/linux_bwrap.py:bwrap_args`), plus `--drop-to`
//! and `--rlimit`, so one mount plan drives both backends and one escape
//! suite covers both.
//!
//! Order of setup, outermost first:
//!   1. rlimits;
//!   2. as root: bounding set emptied, groups cleared, uid/gid set to
//!      `--drop-to` *before* any namespace exists (as root, a mapped uid
//!      writes as host uid 0); root without `--drop-to` is refused;
//!   3. no-new-privs, parent-death signal;
//!   4. one `unshare` of user, mount, pid, ipc, uts, net and cgroup
//!      namespaces, the user namespace mapping our own uid only;
//!   5. pid 1 of the new pid namespace builds a fresh root on a private
//!      tmpfs (sources read through `/oldroot`), pivots into it and
//!      detaches the host root, so nothing is ever mounted in the host
//!      namespace and a kill leaves no mount behind;
//!   6. every capability dropped;
//!   7. the command's own process: new session, seccomp from `--seccomp
//!      FD`, exec. Pid 1 reaps and exits with the command's status, which
//!      tears the namespace (and anything the command left running) down.
//!
//! A setup failure prints `council-sandbox: …` and exits 1 before the
//! command runs; the engine reports it as a setup failure, never as the
//! command's exit code.

use std::ffi::CString;
use std::io::{Error, Read, Write};
use std::os::raw::{c_char, c_int, c_long, c_uint, c_ulong, c_void};
use std::os::unix::fs::{FileTypeExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};

const VERSION: &str = env!("CARGO_PKG_VERSION");

// ------------------------------------------------------------------ libc
#[repr(C)]
struct Rlimit {
    cur: u64,
    max: u64,
}
#[repr(C)]
struct SockFprog {
    len: u16,
    filter: *const u8,
}
#[repr(C)]
struct CapHeader {
    version: u32,
    pid: c_int,
}
#[repr(C)]
#[derive(Clone, Copy)]
struct CapData {
    effective: u32,
    permitted: u32,
    inheritable: u32,
}
#[repr(C)]
struct MountAttr {
    attr_set: u64,
    attr_clr: u64,
    propagation: u64,
    userns_fd: u64,
}

extern "C" {
    fn mount(src: *const c_char, dst: *const c_char, fstype: *const c_char,
             flags: c_ulong, data: *const c_void) -> c_int;
    fn umount2(target: *const c_char, flags: c_int) -> c_int;
    fn unshare(flags: c_int) -> c_int;
    fn prctl(option: c_int, ...) -> c_int;
    fn setresuid(r: u32, e: u32, s: u32) -> c_int;
    fn setresgid(r: u32, e: u32, s: u32) -> c_int;
    fn setgroups(n: usize, list: *const u32) -> c_int;
    fn sethostname(name: *const c_char, len: usize) -> c_int;
    fn setrlimit(res: c_int, lim: *const Rlimit) -> c_int;
    fn fork() -> c_int;
    fn waitpid(pid: c_int, status: *mut c_int, opts: c_int) -> c_int;
    fn _exit(code: c_int) -> !;
    fn execvp(file: *const c_char, argv: *const *const c_char) -> c_int;
    fn setsid() -> c_int;
    fn chdir(path: *const c_char) -> c_int;
    fn pivot_root(new: *const c_char, old: *const c_char) -> c_int;
    fn capset(hdr: *mut CapHeader, data: *const CapData) -> c_int;
    fn syscall(nr: c_long, ...) -> c_long;
    fn getuid() -> u32;
    fn geteuid() -> u32;
    fn getgid() -> u32;
    fn getppid() -> c_int;
}

const CLONE_NEWNS: c_int = 0x0002_0000;
const CLONE_NEWCGROUP: c_int = 0x0200_0000;
const CLONE_NEWUTS: c_int = 0x0400_0000;
const CLONE_NEWIPC: c_int = 0x0800_0000;
const CLONE_NEWUSER: c_int = 0x1000_0000;
const CLONE_NEWPID: c_int = 0x2000_0000;
const CLONE_NEWNET: c_int = 0x4000_0000;

const MS_RDONLY: c_ulong = 1;
const MS_NOSUID: c_ulong = 2;
const MS_NODEV: c_ulong = 4;
const MS_NOEXEC: c_ulong = 8;
const MS_REMOUNT: c_ulong = 32;
const MS_NOATIME: c_ulong = 1024;
const MS_NODIRATIME: c_ulong = 2048;
const MS_BIND: c_ulong = 4096;
const MS_REC: c_ulong = 16384;
const MS_PRIVATE: c_ulong = 1 << 18;
const MS_RELATIME: c_ulong = 1 << 21;
const MS_STRICTATIME: c_ulong = 1 << 24;
const MNT_DETACH: c_int = 2;

const PR_SET_PDEATHSIG: c_int = 1;
const PR_SET_DUMPABLE: c_int = 4;
const PR_SET_SECCOMP: c_int = 22;
const PR_CAPBSET_DROP: c_int = 24;
const PR_SET_NO_NEW_PRIVS: c_int = 38;
const PR_CAP_AMBIENT: c_int = 47;
const PR_CAP_AMBIENT_CLEAR_ALL: c_int = 4;
const SECCOMP_MODE_FILTER: c_ulong = 2;
const SIGKILL: c_int = 9;
const CAP_V3: u32 = 0x2008_0522;

const AT_FDCWD: c_int = -100;
const AT_RECURSIVE: c_uint = 0x8000;
const MOUNT_ATTR_RDONLY: u64 = 1;
const MOUNT_ATTR_NOSUID: u64 = 2;
const MOUNT_ATTR_NODEV: u64 = 4;
const SYS_MOUNT_SETATTR: c_long = 442; // the same on x86_64 and aarch64

#[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]
compile_error!("council-sandbox supports x86_64 and aarch64 (the seccomp tables do too)");

// ------------------------------------------------------------- failures
fn die(what: &str) -> ! {
    let _ = writeln!(std::io::stderr(), "council-sandbox: {}", what);
    unsafe { _exit(1) }
}

fn die_os(what: &str) -> ! {
    die(&format!("{}: {}", what, Error::last_os_error()))
}

fn cs(s: &str) -> CString {
    CString::new(s).unwrap_or_else(|_| die(&format!("NUL byte in {:?}", s)))
}

fn check(rc: c_int, what: &str) {
    if rc != 0 {
        die_os(what)
    }
}

// ----------------------------------------------------------------- args
#[derive(Debug)]
enum Op {
    RoBind { src: String, dst: String, try_: bool },
    Bind { src: String, dst: String },
    Symlink { target: String, path: String },
    Dev(String),
    Proc(String),
    Tmpfs(String),
    RemountRo(String),
}

struct Args {
    ops: Vec<Op>,
    hostname: String,
    chdir: String,
    seccomp_fd: Option<c_int>,
    drop_to: Option<(u32, u32)>,
    rlimits: Vec<(c_int, u64, u64)>,
    argv: Vec<String>,
}

const USAGE: &str = "usage: council-sandbox [--drop-to UID:GID] [--rlimit NAME=SOFT:HARD]...
       [--unshare-all --unshare-user --die-with-parent --new-session]
       [--hostname H] [--ro-bind[-try] SRC DST] [--bind SRC DST]
       [--symlink TARGET PATH] [--dev D] [--proc D] [--tmpfs D]
       [--remount-ro D] [--chdir D] [--seccomp FD] -- COMMAND [ARG]...
Every namespace is always unshared and the network is always private;
the --unshare-* / --die-with-parent / --new-session flags are accepted
for bwrap compatibility and change nothing.";

fn rlimit_of(name: &str) -> c_int {
    match name {
        "fsize" => 1,
        "data" => 2,
        "stack" => 3,
        "core" => 4,
        "nproc" => 6,
        "nofile" => 7,
        "memlock" => 8,
        "as" => 9,
        _ => die(&format!("unknown rlimit {:?}", name)),
    }
}

fn parse() -> Args {
    let mut it = std::env::args().skip(1);
    let mut a = Args {
        ops: vec![],
        hostname: "council".into(),
        chdir: "/".into(),
        seccomp_fd: None,
        drop_to: None,
        rlimits: vec![],
        argv: vec![],
    };
    fn need(it: &mut std::iter::Skip<std::env::Args>, flag: &str) -> String {
        it.next().unwrap_or_else(|| die(&format!("{} needs a value", flag)))
    }
    while let Some(f) = it.next() {
        match f.as_str() {
            "--" => {
                a.argv = it.collect();
                break;
            }
            "--version" => {
                println!("council-sandbox {}", VERSION);
                unsafe { _exit(0) }
            }
            "--help" | "-h" => {
                println!("{}", USAGE);
                unsafe { _exit(0) }
            }
            "--unshare-all" | "--unshare-user" | "--die-with-parent" | "--new-session" => {}
            "--hostname" => a.hostname = need(&mut it, &f),
            "--chdir" => a.chdir = need(&mut it, &f),
            "--seccomp" => {
                let v = need(&mut it, &f);
                a.seccomp_fd = Some(v.parse().unwrap_or_else(|_| die("--seccomp needs an fd")));
            }
            "--drop-to" => {
                let v = need(&mut it, &f);
                let (u, g) = v.split_once(':').unwrap_or_else(|| die("--drop-to UID:GID"));
                let p = |x: &str| x.parse::<u32>().unwrap_or_else(|_| die("--drop-to UID:GID"));
                a.drop_to = Some((p(u), p(g)));
            }
            "--rlimit" => {
                let v = need(&mut it, &f);
                let (n, sh) = v.split_once('=').unwrap_or_else(|| die("--rlimit NAME=SOFT:HARD"));
                let (s, h) = sh.split_once(':').unwrap_or((sh, sh));
                let p = |x: &str| x.parse::<u64>().unwrap_or_else(|_| die("--rlimit NAME=SOFT:HARD"));
                a.rlimits.push((rlimit_of(n), p(s), p(h)));
            }
            "--ro-bind" | "--ro-bind-try" => {
                let src = need(&mut it, &f);
                let dst = need(&mut it, &f);
                a.ops.push(Op::RoBind { src, dst, try_: f == "--ro-bind-try" });
            }
            "--bind" => {
                let src = need(&mut it, &f);
                let dst = need(&mut it, &f);
                a.ops.push(Op::Bind { src, dst });
            }
            "--symlink" => {
                let target = need(&mut it, &f);
                let path = need(&mut it, &f);
                a.ops.push(Op::Symlink { target, path });
            }
            "--dev" => a.ops.push(Op::Dev(need(&mut it, &f))),
            "--proc" => a.ops.push(Op::Proc(need(&mut it, &f))),
            "--tmpfs" => a.ops.push(Op::Tmpfs(need(&mut it, &f))),
            "--remount-ro" => a.ops.push(Op::RemountRo(need(&mut it, &f))),
            other => die(&format!("unknown option {:?}\n{}", other, USAGE)),
        }
    }
    if a.argv.is_empty() {
        die(&format!("no command\n{}", USAGE));
    }
    for p in [&a.chdir] {
        if !p.starts_with('/') {
            die(&format!("{:?} is not absolute", p));
        }
    }
    a
}

// ------------------------------------------------------- privilege drop
fn last_cap() -> u32 {
    std::fs::read_to_string("/proc/sys/kernel/cap_last_cap")
        .ok()
        .and_then(|s| s.trim().parse().ok())
        .unwrap_or(63)
}

/// Empty the bounding set; EINVAL past the kernel's last cap is fine.
fn drop_bounding(strict: bool) {
    for cap in 0..=last_cap() {
        let rc = unsafe { prctl(PR_CAPBSET_DROP, cap as c_ulong, 0 as c_ulong, 0 as c_ulong, 0 as c_ulong) };
        if rc != 0 && strict {
            die_os(&format!("dropping capability {} from the bounding set", cap));
        }
    }
}

fn drop_all_caps() {
    drop_bounding(true);
    unsafe {
        prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL as c_ulong, 0 as c_ulong, 0 as c_ulong, 0 as c_ulong);
    }
    let mut hdr = CapHeader { version: CAP_V3, pid: 0 };
    let zero = [CapData { effective: 0, permitted: 0, inheritable: 0 }; 2];
    check(unsafe { capset(&mut hdr, zero.as_ptr()) }, "clearing capabilities");
}

fn drop_privileges(a: &Args) {
    for &(res, cur, max) in &a.rlimits {
        check(unsafe { setrlimit(res, &Rlimit { cur, max }) }, "setrlimit");
    }
    let root = unsafe { geteuid() } == 0;
    match (root, a.drop_to) {
        (true, None) => die("refusing to run a command as root: pass --drop-to UID:GID"),
        (true, Some((u, g))) => {
            if u == 0 || g == 0 {
                die("--drop-to must not be root");
            }
            drop_bounding(true);
            check(unsafe { setgroups(0, std::ptr::null()) }, "clearing groups");
            check(unsafe { setresgid(g, g, g) }, "setresgid");
            check(unsafe { setresuid(u, u, u) }, "setresuid");
            if unsafe { getuid() } != u || unsafe { geteuid() } != u || unsafe { getgid() } != g {
                die("uid/gid did not change");
            }
            // A credential change clears "dumpable", which makes our own
            // /proc/self/{uid_map,…} root-owned and unwritable; setpriv
            // got this back from the exec into bwrap.
            unsafe { prctl(PR_SET_DUMPABLE, 1 as c_ulong, 0 as c_ulong, 0 as c_ulong, 0 as c_ulong) };
        }
        (false, Some((u, _))) if u != unsafe { geteuid() } => {
            die("--drop-to needs root (running unprivileged already as another uid)")
        }
        _ => {}
    }
    check(unsafe { prctl(PR_SET_NO_NEW_PRIVS, 1 as c_ulong, 0 as c_ulong, 0 as c_ulong, 0 as c_ulong) },
          "no_new_privs");
}

/// `parent` is the pid we expect as ours, or `None` for pid 1 of a new
/// pid namespace, whose parent lives outside it (`getppid()` reads 0).
fn die_with_parent(parent: Option<c_int>) {
    check(unsafe { prctl(PR_SET_PDEATHSIG, SIGKILL as c_ulong, 0 as c_ulong, 0 as c_ulong, 0 as c_ulong) },
          "parent-death signal");
    if parent.map_or(false, |p| unsafe { getppid() } != p) {
        unsafe { _exit(1) } // the parent is already gone
    }
}

fn write_file(path: &str, body: &str) {
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .open(path)
        .unwrap_or_else(|e| die(&format!("open {}: {}", path, e)));
    f.write_all(body.as_bytes()).unwrap_or_else(|e| die(&format!("write {}: {}", path, e)));
}

// --------------------------------------------------------------- mounts
const BASE: &str = "/tmp";
const NEWROOT: &str = "/newroot";
const OLDROOT: &str = "/oldroot";

fn mnt(src: Option<&str>, dst: &str, fstype: Option<&str>, flags: c_ulong, data: Option<&str>) -> Result<(), Error> {
    let s = src.map(cs);
    let d = cs(dst);
    let t = fstype.map(cs);
    let o = data.map(cs);
    let rc = unsafe {
        mount(
            s.as_ref().map_or(std::ptr::null(), |x| x.as_ptr()),
            d.as_ptr(),
            t.as_ref().map_or(std::ptr::null(), |x| x.as_ptr()),
            flags,
            o.as_ref().map_or(std::ptr::null(), |x| x.as_ptr() as *const c_void),
        )
    };
    if rc == 0 { Ok(()) } else { Err(Error::last_os_error()) }
}

/// Resolve `path` (absolute, as seen inside the sandbox) to a host path
/// under NEWROOT, following symlinks *inside* the new root, creating
/// missing directories on the way. The last component is created as a
/// directory or an empty file when `create` says so.
fn inside(path: &str, create: Option<bool>) -> String {
    let mut out = PathBuf::from(NEWROOT);
    let mut todo: Vec<String> = Path::new(path)
        .components()
        .rev()
        .filter_map(|c| match c {
            Component::Normal(s) => Some(s.to_string_lossy().into_owned()),
            Component::ParentDir => Some("..".into()),
            _ => None,
        })
        .collect();
    let mut hops = 0;
    while let Some(c) = todo.pop() {
        if c == ".." {
            if out != Path::new(NEWROOT) {
                out.pop();
            }
            continue;
        }
        let next = out.join(&c);
        let last = todo.is_empty();
        match std::fs::symlink_metadata(&next) {
            Ok(m) if m.file_type().is_symlink() => {
                hops += 1;
                if hops > 40 {
                    die(&format!("too many symlinks resolving {}", path));
                }
                let t = std::fs::read_link(&next).unwrap_or_else(|e| die(&format!("readlink {:?}: {}", next, e)));
                if t.is_absolute() {
                    out = PathBuf::from(NEWROOT);
                }
                for part in t.components().rev() {
                    match part {
                        Component::Normal(s) => todo.push(s.to_string_lossy().into_owned()),
                        Component::ParentDir => todo.push("..".into()),
                        _ => {}
                    }
                }
            }
            Ok(_) => out = next,
            Err(_) => {
                match (last, create) {
                    (true, Some(false)) => {
                        std::fs::File::create(&next).unwrap_or_else(|e| die(&format!("create {:?}: {}", next, e)));
                    }
                    (true, None) => {}
                    _ => {
                        std::fs::create_dir(&next).unwrap_or_else(|e| die(&format!("mkdir {:?}: {}", next, e)));
                        let _ = std::fs::set_permissions(&next, std::fs::Permissions::from_mode(0o755));
                    }
                }
                out = next;
            }
        }
    }
    out.to_string_lossy().into_owned()
}

/// A bind source, canonicalised on the host and read through OLDROOT.
fn source(src: &str) -> Option<(String, bool)> {
    // Not canonicalize(): it would follow an absolute link (resolv.conf
    // -> /run/…) against the current root, the private tmpfs.
    let real = resolve_host(src)?;
    let is_dir = std::fs::metadata(&real).ok()?.is_dir();
    Some((real.to_string_lossy().into_owned(), is_dir))
}

fn resolve_host(src: &str) -> Option<PathBuf> {
    let mut out = PathBuf::from(OLDROOT);
    let mut todo: Vec<String> = src.split('/').filter(|s| !s.is_empty()).rev().map(String::from).collect();
    let mut hops = 0;
    while let Some(c) = todo.pop() {
        if c == ".." {
            if out != Path::new(OLDROOT) {
                out.pop();
            }
            continue;
        }
        let next = out.join(&c);
        let m = std::fs::symlink_metadata(&next).ok()?;
        if m.file_type().is_symlink() {
            hops += 1;
            if hops > 40 {
                return None;
            }
            let t = std::fs::read_link(&next).ok()?;
            if t.is_absolute() {
                out = PathBuf::from(OLDROOT);
            }
            for part in t.to_string_lossy().split('/').filter(|s| !s.is_empty()).rev() {
                todo.push(part.into());
            }
        } else {
            out = next;
        }
    }
    Some(out)
}

/// Per-mount flags of every mount at or under `dst`, from mountinfo.
fn mounts_under(dst: &str) -> Vec<(String, c_ulong)> {
    let info = std::fs::read_to_string("/proc/self/mountinfo").unwrap_or_default();
    let mut out = vec![];
    for line in info.lines() {
        let f: Vec<&str> = line.split(' ').collect();
        if f.len() < 6 {
            continue;
        }
        let mp = unescape(f[4]);
        if mp == dst || mp.starts_with(&format!("{}/", dst.trim_end_matches('/'))) {
            let mut fl = 0;
            for o in f[5].split(',') {
                fl |= match o {
                    "ro" => MS_RDONLY,
                    "nosuid" => MS_NOSUID,
                    "nodev" => MS_NODEV,
                    "noexec" => MS_NOEXEC,
                    "noatime" => MS_NOATIME,
                    "nodiratime" => MS_NODIRATIME,
                    "relatime" => MS_RELATIME,
                    "strictatime" => MS_STRICTATIME,
                    _ => 0,
                };
            }
            out.push((mp, fl));
        }
    }
    out
}

fn unescape(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'\\' && i + 3 < b.len() && b[i + 1..i + 4].iter().all(|c| (b'0'..=b'7').contains(c)) {
            let v = (b[i + 1] - b'0') * 64 + (b[i + 2] - b'0') * 8 + (b[i + 3] - b'0');
            out.push(v);
            i += 4;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Set flags on the mount at `dst` (and every mount under it when
/// `recursive`). `mount_setattr` (5.12+) first; on older kernels, a
/// remount per mount that keeps the flags the kernel locked.
fn set_flags(dst: &str, rdonly: bool, recursive: bool) {
    let set = MOUNT_ATTR_NOSUID | MOUNT_ATTR_NODEV | if rdonly { MOUNT_ATTR_RDONLY } else { 0 };
    let attr = MountAttr { attr_set: set, attr_clr: 0, propagation: 0, userns_fd: 0 };
    let d = cs(dst);
    let rc = unsafe {
        syscall(SYS_MOUNT_SETATTR, AT_FDCWD as c_long, d.as_ptr(),
                if recursive { AT_RECURSIVE } else { 0 } as c_long,
                &attr as *const MountAttr, std::mem::size_of::<MountAttr>() as c_long)
    };
    if rc == 0 {
        return;
    }
    let err = Error::last_os_error();
    if err.raw_os_error() != Some(38) {
        // anything but ENOSYS is a real refusal
        die(&format!("mount_setattr {}: {}", dst, err));
    }
    let targets = if recursive { mounts_under(dst) } else { vec![(dst.to_string(), 0)] };
    let targets = if targets.is_empty() { vec![(dst.to_string(), 0)] } else { targets };
    for (mp, mut fl) in targets {
        if !recursive {
            fl = mounts_under(&mp).into_iter().find(|(m, _)| *m == mp).map_or(0, |x| x.1);
        }
        fl &= !MS_RDONLY;
        let flags = MS_BIND | MS_REMOUNT | MS_NOSUID | MS_NODEV | fl | if rdonly { MS_RDONLY } else { 0 };
        mnt(None, &mp, None, flags, None).unwrap_or_else(|e| die(&format!("remount {}: {}", mp, e)));
    }
}

fn bind(src: &str, dst: &str, rdonly: bool, try_: bool) {
    let Some((real, is_dir)) = source(src) else {
        if try_ {
            return;
        }
        die(&format!("bind source {} is not reachable", src))
    };
    let target = inside(dst, Some(is_dir));
    mnt(Some(&real), &target, None, MS_BIND | MS_REC, None)
        .unwrap_or_else(|e| die(&format!("bind {} -> {}: {}", src, dst, e)));
    set_flags(&target, rdonly, true);
}

fn make_dev(dst: &str) {
    let d = inside(dst, Some(true));
    mnt(Some("tmpfs"), &d, Some("tmpfs"), MS_NOSUID | MS_NOEXEC, Some("mode=0755"))
        .unwrap_or_else(|e| die(&format!("tmpfs on {}: {}", dst, e)));
    for n in ["null", "zero", "full", "random", "urandom", "tty"] {
        let host = format!("{}/dev/{}", OLDROOT, n);
        match std::fs::metadata(&host) {
            Ok(m) if m.file_type().is_char_device() => {}
            _ => continue,
        }
        let node = format!("{}/{}", d, n);
        std::fs::File::create(&node).unwrap_or_else(|e| die(&format!("create {}: {}", node, e)));
        mnt(Some(&host), &node, None, MS_BIND, None).unwrap_or_else(|e| die(&format!("bind /dev/{}: {}", n, e)));
    }
    for (name, target) in [("fd", "/proc/self/fd"), ("stdin", "/proc/self/fd/0"),
                           ("stdout", "/proc/self/fd/1"), ("stderr", "/proc/self/fd/2"),
                           ("core", "/proc/kcore"), ("ptmx", "pts/ptmx")] {
        let _ = std::os::unix::fs::symlink(target, format!("{}/{}", d, name));
    }
    for sub in ["shm", "pts"] {
        let _ = std::fs::create_dir(format!("{}/{}", d, sub));
    }
    // A private devpts for tools that want a pty; not a security layer,
    // so a kernel that refuses it costs only ptys.
    let _ = mnt(Some("devpts"), &format!("{}/pts", d), Some("devpts"), MS_NOSUID | MS_NOEXEC,
                Some("newinstance,ptmxmode=0666,mode=620"));
}

fn make_proc(dst: &str) {
    let d = inside(dst, Some(true));
    mnt(Some("proc"), &d, Some("proc"), MS_NOSUID | MS_NODEV | MS_NOEXEC, None)
        .unwrap_or_else(|e| die(&format!("proc on {}: {}", dst, e)));
    // What bwrap covers too: kernel knobs and triggers stay read-only.
    for sub in ["sys", "sysrq-trigger", "irq", "bus"] {
        let p = format!("{}/{}", d, sub);
        if std::fs::symlink_metadata(&p).is_err() {
            continue;
        }
        mnt(Some(&p), &p, None, MS_BIND | MS_REC, None).unwrap_or_else(|e| die(&format!("bind {}: {}", p, e)));
        set_flags(&p, true, true);
    }
}

fn build_root(a: &Args) {
    // Nothing below may propagate back to the host namespace.
    mnt(None, "/", None, MS_REC | MS_PRIVATE, None).unwrap_or_else(|e| die(&format!("make / private: {}", e)));
    mnt(Some("tmpfs"), BASE, Some("tmpfs"), MS_NOSUID | MS_NODEV, Some("mode=0755"))
        .unwrap_or_else(|e| die(&format!("tmpfs on {}: {}", BASE, e)));
    for d in [NEWROOT, OLDROOT] {
        std::fs::create_dir(format!("{}{}", BASE, d)).unwrap_or_else(|e| die(&format!("mkdir {}: {}", d, e)));
    }
    let (b, o) = (cs(BASE), cs(&format!("{}{}", BASE, OLDROOT)));
    check(unsafe { pivot_root(b.as_ptr(), o.as_ptr()) }, "pivot_root into the private tmpfs");
    check(unsafe { chdir(cs("/").as_ptr()) }, "chdir /");
    mnt(Some("tmpfs"), NEWROOT, Some("tmpfs"), MS_NOSUID | MS_NODEV, Some("mode=0755"))
        .unwrap_or_else(|e| die(&format!("tmpfs on {}: {}", NEWROOT, e)));

    for op in &a.ops {
        match op {
            Op::RoBind { src, dst, try_ } => bind(src, dst, true, *try_),
            Op::Bind { src, dst } => bind(src, dst, false, false),
            Op::Symlink { target, path } => {
                let parent = Path::new(path).parent().map_or("/".into(), |p| p.to_string_lossy().into_owned());
                let p = inside(&parent, Some(true));
                let name = Path::new(path).file_name().unwrap_or_else(|| die("--symlink needs a name"));
                std::os::unix::fs::symlink(target, Path::new(&p).join(name))
                    .unwrap_or_else(|e| die(&format!("symlink {}: {}", path, e)));
            }
            Op::Dev(d) => make_dev(d),
            Op::Proc(d) => make_proc(d),
            Op::Tmpfs(d) => {
                let p = inside(d, Some(true));
                mnt(Some("tmpfs"), &p, Some("tmpfs"), MS_NOSUID | MS_NODEV, Some("mode=0755"))
                    .unwrap_or_else(|e| die(&format!("tmpfs on {}: {}", d, e)));
            }
            Op::RemountRo(d) => {
                let p = if d == "/" { NEWROOT.to_string() } else { inside(d, None) };
                set_flags(&p, true, false);
            }
        }
    }

    // Leave the host root behind: detach it, then make NEWROOT "/".
    let old = cs(OLDROOT);
    check(unsafe { umount2(old.as_ptr(), MNT_DETACH) }, "detaching the host root");
    check(unsafe { chdir(cs(NEWROOT).as_ptr()) }, "chdir into the new root");
    let dot = cs(".");
    check(unsafe { pivot_root(dot.as_ptr(), dot.as_ptr()) }, "pivot_root into the new root");
    check(unsafe { umount2(dot.as_ptr(), MNT_DETACH) }, "detaching the private tmpfs");
    check(unsafe { chdir(cs("/").as_ptr()) }, "chdir /");
}

// ------------------------------------------------------------- seccomp
fn read_filter(fd: c_int) -> Vec<u8> {
    use std::os::unix::io::FromRawFd;
    let mut f = unsafe { std::fs::File::from_raw_fd(fd) };
    let mut prog = vec![];
    f.read_to_end(&mut prog).unwrap_or_else(|e| die(&format!("reading the seccomp program: {}", e)));
    if prog.is_empty() || prog.len() % 8 != 0 || prog.len() / 8 > 4096 {
        die("the seccomp program is not a whole number of BPF instructions");
    }
    prog // the File drops here and closes fd
}

fn load_filter(prog: &[u8]) {
    let fp = SockFprog { len: (prog.len() / 8) as u16, filter: prog.as_ptr() };
    check(unsafe { prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &fp as *const SockFprog, 0 as c_ulong, 0 as c_ulong) },
          "loading the seccomp program");
}

// ------------------------------------------------------------- process
fn wait_status(status: c_int) -> c_int {
    let sig = status & 0x7f;
    if sig == 0 { (status >> 8) & 0xff } else { 128 + sig }
}

fn exec(argv: &[String]) -> ! {
    let c: Vec<CString> = argv.iter().map(|s| cs(s)).collect();
    let mut p: Vec<*const c_char> = c.iter().map(|s| s.as_ptr()).collect();
    p.push(std::ptr::null());
    unsafe { execvp(p[0], p.as_ptr()) };
    let e = Error::last_os_error();
    let _ = writeln!(std::io::stderr(), "council-sandbox: exec {}: {}", argv[0], e);
    unsafe { _exit(if e.raw_os_error() == Some(2) { 127 } else { 126 }) }
}

fn main() {
    let a = parse();
    let launcher_parent = unsafe { getppid() };
    drop_privileges(&a);
    die_with_parent(Some(launcher_parent));

    let filter = a.seccomp_fd.map(read_filter);
    let (uid, gid) = (unsafe { geteuid() }, unsafe { getgid() });
    check(unsafe {
        unshare(CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWPID | CLONE_NEWIPC
                | CLONE_NEWUTS | CLONE_NEWNET | CLONE_NEWCGROUP)
    }, "unshare (user namespaces may be disabled on this host)");
    write_file("/proc/self/setgroups", "deny");
    write_file("/proc/self/uid_map", &format!("{} {} 1\n", uid, uid));
    write_file("/proc/self/gid_map", &format!("{} {} 1\n", gid, gid));

    let init = unsafe { fork() };
    if init < 0 {
        die_os("fork");
    }
    if init > 0 {
        // Outside the pid namespace: hand pid 1's status back to the caller.
        let mut st = 0;
        loop {
            let r = unsafe { waitpid(init, &mut st, 0) };
            if r == init {
                break;
            }
            if Error::last_os_error().raw_os_error() != Some(4) {
                die_os("waitpid");
            }
        }
        unsafe { _exit(wait_status(st)) }
    }

    // Pid 1 of the new namespace.
    die_with_parent(None);
    let host = cs(&a.hostname);
    check(unsafe { sethostname(host.as_ptr(), a.hostname.len()) }, "sethostname");
    build_root(&a);
    drop_all_caps();
    let cwd = cs(&a.chdir);
    check(unsafe { chdir(cwd.as_ptr()) }, &format!("chdir {}", a.chdir));

    let cmd = unsafe { fork() };
    if cmd < 0 {
        die_os("fork");
    }
    if cmd == 0 {
        die_with_parent(Some(1));
        if unsafe { setsid() } < 0 {
            die_os("setsid");
        }
        if let Some(p) = &filter {
            load_filter(p);
        }
        exec(&a.argv);
    }
    // Reap everything; the command's own exit ends the namespace.
    loop {
        let mut st = 0;
        let r = unsafe { waitpid(-1, &mut st, 0) };
        if r == cmd {
            unsafe { _exit(wait_status(st)) }
        }
        if r < 0 && Error::last_os_error().raw_os_error() != Some(4) {
            unsafe { _exit(1) }
        }
    }
}

