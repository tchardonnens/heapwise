//! Linux: see native allocations by rewriting GOT entries.
//!
//! Every shared library calls `malloc`, `free`, `mmap` and friends through its
//! Global Offset Table. We point those entries at our wrappers, which call the
//! real functions and report to the recorder, the same technique memray uses.
//! No `LD_PRELOAD`, so it works after the interpreter has started.
//!
//! Which objects are patched:
//! - C extensions and the libraries they pull in (numpy, OpenBLAS, Arrow,
//!   libstdc++ for `operator new`, ...): all allocation functions.
//! - The interpreter itself (the main executable or libpython): only `dlopen`,
//!   so extensions imported later get patched too. Its own allocations are
//!   already seen through the PyMem hooks; patching its `malloc` would count
//!   them twice.
//! - libc, the dynamic loader, the vDSO and heapwise itself: never.

use std::ffi::CStr;
use std::os::raw::{c_char, c_int, c_void};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;

use libc::{off_t, size_t};

use crate::hooks::{on_alloc, on_free, Source};

#[repr(C)]
struct Dyn {
    tag: i64,
    val: u64,
}

#[repr(C)]
struct Rela {
    offset: u64,
    info: u64,
    addend: i64,
}

#[repr(C)]
struct Sym {
    name: u32,
    info: u8,
    other: u8,
    shndx: u16,
    value: u64,
    size: u64,
}

const DT_NULL: i64 = 0;
const DT_PLTRELSZ: i64 = 2;
const DT_STRTAB: i64 = 5;
const DT_SYMTAB: i64 = 6;
const DT_RELA: i64 = 7;
const DT_RELASZ: i64 = 8;
const DT_JMPREL: i64 = 23;

const PT_LOAD: u32 = 1;
const PT_DYNAMIC: u32 = 2;
const PT_GNU_RELRO: u32 = 0x6474_e552;

#[cfg(target_arch = "x86_64")]
const RELOC_TYPES: [u32; 2] = [7, 6]; // R_X86_64_JUMP_SLOT, R_X86_64_GLOB_DAT
#[cfg(target_arch = "aarch64")]
const RELOC_TYPES: [u32; 2] = [1026, 1025]; // R_AARCH64_JUMP_SLOT, R_AARCH64_GLOB_DAT

extern "C" {
    fn memalign(align: size_t, size: size_t) -> *mut c_void;
    fn aligned_alloc(align: size_t, size: size_t) -> *mut c_void;
    fn valloc(size: size_t) -> *mut c_void;
    fn pvalloc(size: size_t) -> *mut c_void;
    fn reallocarray(ptr: *mut c_void, nmemb: size_t, size: size_t) -> *mut c_void;
    fn pthread_atfork(
        prepare: Option<unsafe extern "C" fn()>,
        parent: Option<unsafe extern "C" fn()>,
        child: Option<unsafe extern "C" fn()>,
    ) -> c_int;
}

#[derive(Clone, Copy, PartialEq)]
enum Scope {
    Skip,
    DlopenOnly,
    All,
}

struct Patch {
    slot: usize,
    original: usize,
    ours: usize,
}

static INSTALLED: AtomicBool = AtomicBool::new(false);
/// The loader's load counter (`dlpi_adds`) when we last patched.
static LOAD_GENERATION: AtomicU64 = AtomicU64::new(0);
static PATCHES: Mutex<Vec<Patch>> = Mutex::new(Vec::new());

fn hook_table() -> [(&'static [u8], usize); 13] {
    [
        (b"malloc", hw_malloc as *const () as usize),
        (b"calloc", hw_calloc as *const () as usize),
        (b"realloc", hw_realloc as *const () as usize),
        (b"reallocarray", hw_reallocarray as *const () as usize),
        (b"free", hw_free as *const () as usize),
        (b"posix_memalign", hw_posix_memalign as *const () as usize),
        (b"aligned_alloc", hw_aligned_alloc as *const () as usize),
        (b"memalign", hw_memalign as *const () as usize),
        (b"valloc", hw_valloc as *const () as usize),
        (b"pvalloc", hw_pvalloc as *const () as usize),
        (b"mmap", hw_mmap as *const () as usize),
        (b"mmap64", hw_mmap as *const () as usize),
        (b"munmap", hw_munmap as *const () as usize),
    ]
}

pub fn install() -> bool {
    unsafe {
        static ATFORK: std::sync::Once = std::sync::Once::new();
        ATFORK.call_once(|| {
            pthread_atfork(None, None, Some(crate::hooks::after_fork_in_child));
        });
        INSTALLED.store(true, Ordering::SeqCst);
        patch_all();
    }
    true
}

pub fn uninstall() {
    INSTALLED.store(false, Ordering::SeqCst);
    let mut patches = PATCHES.lock().unwrap_or_else(|e| e.into_inner());
    let loaded = loaded_ranges();
    for p in patches.drain(..) {
        // Skip libraries that were unloaded while we were recording.
        if !loaded.iter().any(|&(lo, hi)| p.slot >= lo && p.slot + 8 <= hi) {
            continue;
        }
        unsafe {
            if (p.slot as *const usize).read_volatile() == p.ours {
                write_slot(p.slot, p.original, &loaded_relro_of(p.slot));
            }
        }
    }
}

fn own_object_name() -> Option<Vec<u8>> {
    unsafe {
        let mut info: libc::Dl_info = std::mem::zeroed();
        if libc::dladdr(hw_malloc as *const c_void, &mut info) == 0 || info.dli_fname.is_null() {
            return None;
        }
        Some(CStr::from_ptr(info.dli_fname).to_bytes().to_vec())
    }
}

fn scope_for(name: &[u8], own: &Option<Vec<u8>>) -> Scope {
    if name.is_empty() {
        return Scope::DlopenOnly; // the main executable
    }
    if own.as_deref() == Some(name) {
        return Scope::Skip;
    }
    let contains = |needle: &[u8]| name.windows(needle.len()).any(|w| w == needle);
    const SKIP: [&[u8]; 8] = [
        b"linux-vdso",
        b"linux-gate",
        b"/ld-linux",
        b"ld-musl",
        b"/libc.so",
        b"/libc.musl",
        b"/libpthread.so",
        b"/libdl.so",
    ];
    if SKIP.iter().any(|s| contains(s)) {
        return Scope::Skip;
    }
    if contains(b"/libpython") {
        return Scope::DlopenOnly;
    }
    Scope::All
}

struct WalkCtx {
    own: Option<Vec<u8>>,
}

/// Patch libraries loaded since the last pass. `dlopen` from the interpreter
/// (imports) is hooked directly; this catches the rest, such as `ctypes.CDLL`
/// or a library loading a plugin. We do not hook `dlopen` there because
/// calling it from heapwise would change which RUNPATH the loader searches.
pub fn poll() {
    if !INSTALLED.load(Ordering::Relaxed) {
        return;
    }
    let adds = load_generation();
    if adds != LOAD_GENERATION.load(Ordering::Relaxed) {
        unsafe { patch_all() };
    }
}

fn load_generation() -> u64 {
    unsafe extern "C" fn cb(info: *mut libc::dl_phdr_info, size: size_t, data: *mut c_void) -> c_int {
        let needed = std::mem::offset_of!(libc::dl_phdr_info, dlpi_adds) + 8;
        if size >= needed {
            *(data as *mut u64) = (*info).dlpi_adds;
        }
        1 // the counter is the same on every entry; stop after the first
    }
    let mut adds = 0u64;
    unsafe { libc::dl_iterate_phdr(Some(cb), &mut adds as *mut u64 as *mut c_void) };
    adds
}

unsafe fn patch_all() {
    LOAD_GENERATION.store(load_generation(), Ordering::Relaxed);
    let mut ctx = WalkCtx { own: own_object_name() };
    libc::dl_iterate_phdr(Some(patch_callback), &mut ctx as *mut WalkCtx as *mut c_void);
}

unsafe extern "C" fn patch_callback(info: *mut libc::dl_phdr_info, _size: size_t, data: *mut c_void) -> c_int {
    let ctx = &*(data as *const WalkCtx);
    let info = &*info;
    let name = if info.dlpi_name.is_null() { &b""[..] } else { CStr::from_ptr(info.dlpi_name).to_bytes() };
    let scope = scope_for(name, &ctx.own);
    if scope != Scope::Skip {
        patch_object(info, scope);
    }
    0
}

unsafe fn phdrs(info: &libc::dl_phdr_info) -> &[libc::Elf64_Phdr] {
    if info.dlpi_phdr.is_null() {
        return &[];
    }
    std::slice::from_raw_parts(info.dlpi_phdr, info.dlpi_phnum as usize)
}

unsafe fn patch_object(info: &libc::dl_phdr_info, scope: Scope) {
    let base = info.dlpi_addr as usize;
    let mut dynamic: *const Dyn = std::ptr::null();
    let mut relro = Vec::new();
    for ph in phdrs(info) {
        match ph.p_type {
            PT_DYNAMIC => dynamic = (base + ph.p_vaddr as usize) as *const Dyn,
            PT_GNU_RELRO => relro.push((base + ph.p_vaddr as usize, base + (ph.p_vaddr + ph.p_memsz) as usize)),
            _ => {}
        }
    }
    if dynamic.is_null() {
        return;
    }

    let (mut strtab, mut symtab, mut jmprel, mut pltrelsz, mut rela, mut relasz) = (0, 0, 0, 0, 0, 0);
    // glibc relocates these in place, musl does not.
    let fix = |v: u64| if (v as usize) < base { base + v as usize } else { v as usize };
    let mut d = dynamic;
    while (*d).tag != DT_NULL {
        match (*d).tag {
            DT_STRTAB => strtab = fix((*d).val),
            DT_SYMTAB => symtab = fix((*d).val),
            DT_JMPREL => jmprel = fix((*d).val),
            DT_PLTRELSZ => pltrelsz = (*d).val as usize,
            DT_RELA => rela = fix((*d).val),
            DT_RELASZ => relasz = (*d).val as usize,
            _ => {}
        }
        d = d.add(1);
    }
    if strtab == 0 || symtab == 0 {
        return;
    }

    let table = hook_table();
    let mut patches = PATCHES.lock().unwrap_or_else(|e| e.into_inner());
    for (start, size) in [(jmprel, pltrelsz), (rela, relasz)] {
        if start == 0 || size == 0 {
            continue;
        }
        let relocs = std::slice::from_raw_parts(start as *const Rela, size / std::mem::size_of::<Rela>());
        for r in relocs {
            if !RELOC_TYPES.contains(&((r.info & 0xffff_ffff) as u32)) {
                continue;
            }
            let sym_index = (r.info >> 32) as usize;
            if sym_index == 0 {
                continue;
            }
            let sym = &*(symtab as *const Sym).add(sym_index);
            let name = CStr::from_ptr((strtab + sym.name as usize) as *const c_char).to_bytes();
            let ours = if name == b"dlopen" {
                hw_dlopen as *const () as usize
            } else if scope == Scope::All {
                match table.iter().find(|(n, _)| *n == name) {
                    Some(&(_, f)) => f,
                    None => continue,
                }
            } else {
                continue;
            };
            let slot = base + r.offset as usize;
            let current = (slot as *const usize).read_volatile();
            if current == ours {
                continue; // already patched
            }
            if write_slot(slot, ours, &relro) {
                patches.push(Patch { slot, original: current, ours });
            }
        }
    }
}

/// Overwrite one GOT entry, lifting RELRO protection around the write.
unsafe fn write_slot(slot: usize, value: usize, relro: &[(usize, usize)]) -> bool {
    let page_size = libc::sysconf(libc::_SC_PAGESIZE) as usize;
    let page = slot & !(page_size - 1);
    let len = if (slot + 8) > page + page_size { 2 * page_size } else { page_size };
    if libc::mprotect(page as *mut c_void, len, libc::PROT_READ | libc::PROT_WRITE) != 0 {
        return false;
    }
    // An aligned pointer-sized store is atomic, so threads calling through
    // this entry see either the old or the new function.
    (slot as *mut usize).write_volatile(value);
    if relro.iter().any(|&(lo, hi)| slot >= lo && slot < hi) {
        libc::mprotect(page as *mut c_void, len, libc::PROT_READ);
    }
    true
}

fn loaded_ranges() -> Vec<(usize, usize)> {
    unsafe extern "C" fn cb(info: *mut libc::dl_phdr_info, _: size_t, data: *mut c_void) -> c_int {
        let out = &mut *(data as *mut Vec<(usize, usize)>);
        let info = &*info;
        let base = info.dlpi_addr as usize;
        for ph in phdrs(info) {
            if ph.p_type == PT_LOAD {
                out.push((base + ph.p_vaddr as usize, base + (ph.p_vaddr + ph.p_memsz) as usize));
            }
        }
        0
    }
    let mut out: Vec<(usize, usize)> = Vec::new();
    unsafe { libc::dl_iterate_phdr(Some(cb), &mut out as *mut _ as *mut c_void) };
    out
}

fn loaded_relro_of(slot: usize) -> Vec<(usize, usize)> {
    unsafe extern "C" fn cb(info: *mut libc::dl_phdr_info, _: size_t, data: *mut c_void) -> c_int {
        let (slot, out) = &mut *(data as *mut (usize, Vec<(usize, usize)>));
        let info = &*info;
        let base = info.dlpi_addr as usize;
        for ph in phdrs(info) {
            if ph.p_type == PT_GNU_RELRO {
                let range = (base + ph.p_vaddr as usize, base + (ph.p_vaddr + ph.p_memsz) as usize);
                if *slot >= range.0 && *slot < range.1 {
                    out.push(range);
                }
            }
        }
        0
    }
    let mut data = (slot, Vec::new());
    unsafe { libc::dl_iterate_phdr(Some(cb), &mut data as *mut _ as *mut c_void) };
    data.1
}

// Wrappers. Each calls the real function (resolved through heapwise's own,
// unpatched GOT) and then reports.

unsafe extern "C" fn hw_malloc(size: size_t) -> *mut c_void {
    let p = libc::malloc(size);
    if !p.is_null() {
        on_alloc(Source::Native, p as usize, size);
    }
    p
}

unsafe extern "C" fn hw_calloc(n: size_t, size: size_t) -> *mut c_void {
    let p = libc::calloc(n, size);
    if !p.is_null() {
        on_alloc(Source::Native, p as usize, n.saturating_mul(size));
    }
    p
}

unsafe extern "C" fn hw_realloc(ptr: *mut c_void, size: size_t) -> *mut c_void {
    if !ptr.is_null() {
        on_free(ptr as usize);
    }
    let p = libc::realloc(ptr, size);
    if !p.is_null() {
        on_alloc(Source::Native, p as usize, size);
    }
    p
}

unsafe extern "C" fn hw_reallocarray(ptr: *mut c_void, n: size_t, size: size_t) -> *mut c_void {
    if !ptr.is_null() {
        on_free(ptr as usize);
    }
    let p = reallocarray(ptr, n, size);
    if !p.is_null() {
        on_alloc(Source::Native, p as usize, n.saturating_mul(size));
    }
    p
}

unsafe extern "C" fn hw_free(ptr: *mut c_void) {
    if !ptr.is_null() {
        on_free(ptr as usize);
    }
    libc::free(ptr);
}

unsafe extern "C" fn hw_posix_memalign(out: *mut *mut c_void, align: size_t, size: size_t) -> c_int {
    let rc = libc::posix_memalign(out, align, size);
    if rc == 0 && !(*out).is_null() {
        on_alloc(Source::Native, *out as usize, size);
    }
    rc
}

unsafe extern "C" fn hw_aligned_alloc(align: size_t, size: size_t) -> *mut c_void {
    let p = aligned_alloc(align, size);
    if !p.is_null() {
        on_alloc(Source::Native, p as usize, size);
    }
    p
}

unsafe extern "C" fn hw_memalign(align: size_t, size: size_t) -> *mut c_void {
    let p = memalign(align, size);
    if !p.is_null() {
        on_alloc(Source::Native, p as usize, size);
    }
    p
}

unsafe extern "C" fn hw_valloc(size: size_t) -> *mut c_void {
    let p = valloc(size);
    if !p.is_null() {
        on_alloc(Source::Native, p as usize, size);
    }
    p
}

unsafe extern "C" fn hw_pvalloc(size: size_t) -> *mut c_void {
    let p = pvalloc(size);
    if !p.is_null() {
        on_alloc(Source::Native, p as usize, size);
    }
    p
}

unsafe extern "C" fn hw_mmap(
    addr: *mut c_void,
    len: size_t,
    prot: c_int,
    flags: c_int,
    fd: c_int,
    offset: off_t,
) -> *mut c_void {
    let p = libc::mmap(addr, len, prot, flags, fd, offset);
    // Only anonymous memory; file mappings are page cache, not heap.
    if p != libc::MAP_FAILED && flags & libc::MAP_ANONYMOUS != 0 {
        on_alloc(Source::Native, p as usize, len);
    }
    p
}

unsafe extern "C" fn hw_munmap(addr: *mut c_void, len: size_t) -> c_int {
    // Partial unmaps of a recorded mapping are not tracked yet.
    on_free(addr as usize);
    libc::munmap(addr, len)
}

unsafe extern "C" fn hw_dlopen(file: *const c_char, flags: c_int) -> *mut c_void {
    let handle = libc::dlopen(file, flags);
    if !handle.is_null() && INSTALLED.load(Ordering::SeqCst) {
        patch_all();
    }
    handle
}
