//! Terminal report: findings in plain language first, evidence after.

use std::fmt::Write;

use crate::analysis::{Capture, SiteRow, NO_STACK};

pub fn human_bytes(n: u64) -> String {
    const UNITS: [&str; 5] = ["B", "KiB", "MiB", "GiB", "TiB"];
    let mut v = n as f64;
    let mut u = 0;
    while v >= 1024.0 && u < UNITS.len() - 1 {
        v /= 1024.0;
        u += 1;
    }
    if u == 0 {
        format!("{n} B")
    } else {
        format!("{v:.1} {}", UNITS[u])
    }
}

fn short_path(file: &str, cwd: Option<&str>) -> String {
    if let Some(cwd) = cwd {
        let cwd = cwd.trim_end_matches(['/', '\\']);
        if let Some(rest) = file.strip_prefix(cwd) {
            let rest = rest.trim_start_matches(['/', '\\']);
            if !rest.is_empty() {
                return rest.to_string();
            }
        }
    }
    file.to_string()
}

fn location(r: &SiteRow, cwd: Option<&str>) -> String {
    if r.file.is_empty() {
        return String::new();
    }
    format!("{}:{}", short_path(&r.file, cwd), r.line)
}

/// "load_rows() at etl.py:7", or a plain description when there is no
/// Python frame (library-owned threads, or calls that released the GIL).
fn culprit(r: &SiteRow, cwd: Option<&str>) -> String {
    if r.func == NO_STACK {
        "native code with no Python stack (a library's own threads, or a call that released the GIL)".into()
    } else if r.file.is_empty() {
        format!("{} (the interpreter loading your script)", r.func)
    } else {
        format!("{}() at {}", r.func, location(r, cwd))
    }
}

/// 1049111 -> "1,049,111".
fn grouped(n: u64) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

fn sparkline(values: &[u64]) -> String {
    const BARS: [char; 8] = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];
    let max = values.iter().copied().max().unwrap_or(0).max(1);
    values
        .iter()
        .map(|&v| BARS[((v as u128 * 7 + max as u128 / 2) / max as u128) as usize])
        .collect()
}

fn pct(part: u64, whole: u64) -> f64 {
    if whole == 0 {
        0.0
    } else {
        part as f64 * 100.0 / whole as f64
    }
}

pub fn summary(cap: &Capture, title: &str, exclude: &[String], cwd: Option<&str>, top: usize) -> String {
    let mut out = String::new();
    let mode = if cap.sample_rate == 0 {
        "every allocation".to_string()
    } else {
        format!("sampled, one per {} on average", human_bytes(cap.sample_rate))
    };
    let coverage = if cap.native_supported {
        "Python + native"
    } else {
        "malloc/mmap from C extensions not tracked on this OS yet"
    };
    let _ = writeln!(out, "heapwise · {title} · {:.2} s · {mode} · {coverage}", cap.duration_ns as f64 / 1e9);
    let _ = writeln!(out);

    let peak_rows = cap.rows("peak", exclude);
    let leak_rows = cap.rows("leaks", exclude);
    let temp_rows = cap.rows("temporaries", exclude);
    let leaked: u64 = cap.live_at_end.values().map(|a| a.bytes).sum();

    // Findings.
    if cap.peak_bytes == 0 {
        let _ = writeln!(out, "No Python heap allocations were recorded.");
        return out;
    }
    let split = {
        format!(
            " ({} native, {} Python)",
            human_bytes(cap.peak_native_bytes),
            human_bytes(cap.peak_bytes - cap.peak_native_bytes.min(cap.peak_bytes))
        )
    };
    let mut line = format!(
        "Peak {}{split} at {:.2} s.",
        human_bytes(cap.peak_bytes),
        cap.peak_t_ns as f64 / 1e9
    );
    if let Some(r) = peak_rows.first() {
        let _ = write!(
            line,
            " {:.0}% of it was allocated by {}.",
            pct(r.bytes, cap.peak_bytes),
            culprit(r, cwd)
        );
    }
    let _ = writeln!(out, "{line}");
    if let Some(r) = leak_rows.first().filter(|_| leaked > 0) {
        let _ = writeln!(
            out,
            "Still allocated at exit: {}, most of it from {}.",
            human_bytes(leaked),
            culprit(r, cwd)
        );
    }
    if let Some(r) = temp_rows.first() {
        let total: u64 = temp_rows.iter().map(|r| r.count).sum();
        let _ = writeln!(
            out,
            "{} temporary allocations (freed right after being made); {} made {}.",
            grouped(total),
            culprit(r, cwd),
            grouped(r.count)
        );
    }
    let tl = cap.timeline_values(48);
    let _ = writeln!(out, "Memory over time  {}  peak {}", sparkline(&tl), human_bytes(cap.peak_bytes));

    // Evidence.
    let section = |out: &mut String, heading: &str, rows: &[SiteRow], whole: u64, bytes: bool| {
        if rows.is_empty() {
            return;
        }
        let _ = writeln!(out);
        let _ = writeln!(out, "{heading}");
        let width = rows.iter().take(top).map(|r| r.func.len()).max().unwrap_or(0).min(40);
        let any_native = rows.iter().take(top).any(|r| r.native_bytes > 0);
        for r in rows.iter().take(top) {
            let func: String = r.func.chars().take(40).collect();
            if bytes {
                let native = match (any_native, r.native_bytes) {
                    (false, _) => String::new(),
                    (true, 0) => format!("  {:>17}", ""),
                    (true, n) => format!("  {:>17}", format!("{} native", human_bytes(n))),
                };
                let _ = writeln!(
                    out,
                    "  {:>5.1}%  {:>10}{native}  {:<width$}  {}",
                    pct(r.bytes, whole),
                    human_bytes(r.bytes),
                    func,
                    location(r, cwd),
                );
            } else {
                let _ = writeln!(out, "  {:>10}×  {:<width$}  {}", grouped(r.count), func, location(r, cwd));
            }
        }
    };
    section(&mut out, "Peak: what was live at the high-water mark", &peak_rows, cap.peak_bytes, true);
    section(&mut out, "Leaks: still allocated at exit", &leak_rows, leaked, true);
    if cap.sample_rate == 0 {
        section(&mut out, "Temporaries: freed right after allocation", &temp_rows, 0, false);
    } else {
        let _ = writeln!(out);
        let _ = writeln!(out, "Temporaries are counted with --exact.");
    }
    out
}
