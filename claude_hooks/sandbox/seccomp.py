"""The council sandbox's seccomp denylist, as a classic-BPF program.

Layer 7 of ``docs/research/cerebriline-map/04-sandbox-shell.md`` §5.3.
Default allow; the syscalls that reach the kernel surface a jailed
command has no business touching return ``EPERM``. A foreign
architecture or an x32 call is killed, because a denylist keyed on
x86_64 numbers says nothing about another table.

Generated here, with no helper binary and no library: bwrap reads the
raw program from an fd (``--seccomp FD``), and the program is ~100
instructions of fixed shape. Every block is self-contained — it ends in
a ``ret`` — so no jump ever crosses another block and no offset is ever
wider than the block itself (classic BPF jumps are 8-bit).
"""
from __future__ import annotations

import platform
import struct

# ---- BPF opcodes (linux/filter.h) ----------------------------------------- #
_LD_W_ABS = 0x20          # BPF_LD | BPF_W | BPF_ABS
_JEQ_K = 0x15             # BPF_JMP | BPF_JEQ | BPF_K
_JGE_K = 0x35             # BPF_JMP | BPF_JGE | BPF_K
_JSET_K = 0x45            # BPF_JMP | BPF_JSET | BPF_K
_RET_K = 0x06             # BPF_RET | BPF_K

# ---- seccomp return values (linux/seccomp.h) ------------------------------ #
RET_KILL_PROCESS = 0x80000000
RET_ERRNO = 0x00050000
RET_ALLOW = 0x7FFF0000
EPERM = 1
ENOSYS = 38

# ---- struct seccomp_data offsets ------------------------------------------ #
_NR, _ARCH = 0, 4


def _arg_lo(i: int) -> int:
    """Low 32 bits of ``args[i]`` (both supported arches are little-endian)."""
    return 16 + 8 * i


AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
X32_SYSCALL_BIT = 0x40000000

#: Every ``CLONE_NEW*`` flag: NEWTIME, NEWNS, NEWCGROUP, NEWUTS, NEWIPC,
#: NEWUSER, NEWPID, NEWNET. All in the low word of the flags argument.
CLONE_NEW_MASK = (0x00000080 | 0x00020000 | 0x02000000 | 0x04000000
                  | 0x08000000 | 0x10000000 | 0x20000000 | 0x40000000)
TIOCSTI, TIOCLINUX = 0x5412, 0x541C
AF_NETLINK, AF_PACKET = 16, 17
NETLINK_ROUTE = 0

#: What is denied, by name. Grouped as in the design (§5.3 layer 7).
DENIED = (
    # mount family — a fresh mount would undo the view
    "mount", "umount2", "pivot_root", "move_mount", "fsopen", "fsconfig",
    "fsmount", "fspick", "open_tree", "mount_setattr", "chroot",
    # namespaces — no nested userns, so no nft, no mount, no new netns
    "unshare", "setns",
    # tracing and other processes' memory
    "ptrace", "process_vm_readv", "process_vm_writev", "pidfd_getfd",
    "userfaultfd", "perf_event_open", "bpf",
    # kernel and modules
    "kexec_load", "kexec_file_load", "init_module", "finit_module",
    "delete_module",
    # keyrings
    "add_key", "request_key", "keyctl",
    # io_uring: ring operations bypass syscall filtering entirely
    "io_uring_setup", "io_uring_enter", "io_uring_register",
    # file handles walk past the mount tree
    "open_by_handle_at", "name_to_handle_at",
    # system
    "swapon", "swapoff", "reboot", "acct", "quotactl", "quotactl_fd",
    "iopl", "ioperm", "syslog", "settimeofday", "clock_settime",
)

#: Syscall numbers per architecture. aarch64 uses the asm-generic table
#: and has no ``iopl`` / ``ioperm``.
SYSCALLS: dict[str, dict[str, int]] = {
    "x86_64": {
        "mount": 165, "umount2": 166, "pivot_root": 155, "move_mount": 429,
        "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433,
        "open_tree": 428, "mount_setattr": 442, "chroot": 161,
        "unshare": 272, "setns": 308,
        "ptrace": 101, "process_vm_readv": 310, "process_vm_writev": 311,
        "pidfd_getfd": 438, "userfaultfd": 323, "perf_event_open": 298,
        "bpf": 321,
        "kexec_load": 246, "kexec_file_load": 320, "init_module": 175,
        "finit_module": 313, "delete_module": 176,
        "add_key": 248, "request_key": 249, "keyctl": 250,
        "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427,
        "open_by_handle_at": 304, "name_to_handle_at": 303,
        "swapon": 167, "swapoff": 168, "reboot": 169, "acct": 163,
        "quotactl": 179, "quotactl_fd": 443, "iopl": 172, "ioperm": 173,
        "syslog": 103, "settimeofday": 164, "clock_settime": 227,
        # argument-checked
        "clone": 56, "clone3": 435, "ioctl": 16, "socket": 41,
    },
    "aarch64": {
        "mount": 40, "umount2": 39, "pivot_root": 41, "move_mount": 429,
        "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433,
        "open_tree": 428, "mount_setattr": 442, "chroot": 51,
        "unshare": 97, "setns": 268,
        "ptrace": 117, "process_vm_readv": 270, "process_vm_writev": 271,
        "pidfd_getfd": 438, "userfaultfd": 282, "perf_event_open": 241,
        "bpf": 280,
        "kexec_load": 104, "kexec_file_load": 294, "init_module": 105,
        "finit_module": 273, "delete_module": 106,
        "add_key": 217, "request_key": 218, "keyctl": 219,
        "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427,
        "open_by_handle_at": 265, "name_to_handle_at": 264,
        "swapon": 224, "swapoff": 225, "reboot": 142, "acct": 89,
        "quotactl": 60, "quotactl_fd": 443,
        "syslog": 116, "settimeofday": 170, "clock_settime": 112,
        "clone": 220, "clone3": 435, "ioctl": 29, "socket": 198,
    },
}


def native_arch() -> str:
    m = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(m, m)


def _ins(code: int, jt: int = 0, jf: int = 0, k: int = 0) -> tuple:
    assert 0 <= jt <= 255 and 0 <= jf <= 255
    return (code, jt, jf, k & 0xFFFFFFFF)


def instructions(arch: str | None = None) -> list[tuple]:
    """The program as ``(code, jt, jf, k)`` tuples, for tests and dumps."""
    arch = arch or native_arch()
    if arch not in SYSCALLS:
        raise ValueError(f"no seccomp table for {arch!r}")
    nr = SYSCALLS[arch]
    eperm = RET_ERRNO | EPERM
    p = [
        _ins(_LD_W_ABS, k=_ARCH),
        _ins(_JEQ_K, 1, 0, AUDIT_ARCH[arch]),
        _ins(_RET_K, k=RET_KILL_PROCESS),
        _ins(_LD_W_ABS, k=_NR),
    ]
    if arch == "x86_64":
        p += [_ins(_JGE_K, 0, 1, X32_SYSCALL_BIT), _ins(_RET_K, k=RET_KILL_PROCESS)]
    for name in DENIED:
        if name in nr:
            p += [_ins(_JEQ_K, 0, 1, nr[name]), _ins(_RET_K, k=eperm)]
    # clone3 hides its flags behind a pointer: refuse it as unimplemented,
    # and libc falls back to clone, whose flags we can read.
    p += [_ins(_JEQ_K, 0, 1, nr["clone3"]), _ins(_RET_K, k=RET_ERRNO | ENOSYS)]
    # clone with any CLONE_NEW* flag
    p += [
        _ins(_JEQ_K, 0, 4, nr["clone"]),
        _ins(_LD_W_ABS, k=_arg_lo(0)),
        _ins(_JSET_K, 0, 1, CLONE_NEW_MASK),
        _ins(_RET_K, k=eperm),
        _ins(_RET_K, k=RET_ALLOW),
    ]
    # ioctl(TIOCSTI | TIOCLINUX): typing into another terminal
    p += [
        _ins(_JEQ_K, 0, 5, nr["ioctl"]),
        _ins(_LD_W_ABS, k=_arg_lo(1)),
        _ins(_JEQ_K, 2, 0, TIOCSTI),
        _ins(_JEQ_K, 1, 0, TIOCLINUX),
        _ins(_RET_K, k=RET_ALLOW),
        _ins(_RET_K, k=eperm),
    ]
    # socket(AF_PACKET) and socket(AF_NETLINK) other than NETLINK_ROUTE
    p += [
        _ins(_JEQ_K, 0, 7, nr["socket"]),
        _ins(_LD_W_ABS, k=_arg_lo(0)),
        _ins(_JEQ_K, 4, 0, AF_PACKET),
        _ins(_JEQ_K, 0, 2, AF_NETLINK),
        _ins(_LD_W_ABS, k=_arg_lo(2)),
        _ins(_JEQ_K, 0, 1, NETLINK_ROUTE),
        _ins(_RET_K, k=RET_ALLOW),
        _ins(_RET_K, k=eperm),
    ]
    p.append(_ins(_RET_K, k=RET_ALLOW))
    return p


def build_filter(arch: str | None = None) -> bytes:
    """The raw ``struct sock_filter[]`` bwrap's ``--seccomp`` reads."""
    return b"".join(struct.pack("=HBBI", *i) for i in instructions(arch))


def evaluate(prog: list[tuple], *, nr: int, arch: int,
             args: tuple[int, ...] = ()) -> int:
    """Run ``prog`` on one syscall — a tiny interpreter for the tests,
    so the program's logic is checked without loading it."""
    args = tuple(args) + (0,) * (6 - len(args))
    data = {_NR: nr, _ARCH: arch}
    for i, a in enumerate(args):
        data[_arg_lo(i)] = a & 0xFFFFFFFF
    acc, pc = 0, 0
    while True:
        code, jt, jf, k = prog[pc]
        if code == _LD_W_ABS:
            acc = data.get(k, 0)
        elif code == _RET_K:
            return k
        elif code in (_JEQ_K, _JGE_K, _JSET_K):
            hit = (acc == k if code == _JEQ_K else
                   acc >= k if code == _JGE_K else bool(acc & k))
            pc += jt if hit else jf
        else:  # pragma: no cover - the generator emits nothing else
            raise ValueError(f"opcode {code:#x}")
        pc += 1


__all__ = ["DENIED", "SYSCALLS", "build_filter", "instructions", "evaluate",
           "native_arch", "RET_ALLOW", "RET_ERRNO", "RET_KILL_PROCESS",
           "EPERM", "ENOSYS"]
