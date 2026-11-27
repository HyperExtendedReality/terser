//! Byte-exact port of CPython's `difflib.SequenceMatcher` (no junk function, `autojunk=True`)
//! and `difflib.unified_diff`, so native Tier 1 read deltas match the Python runtime exactly.

use std::collections::HashMap;

/// `str.splitlines(keepends=True)`: splits on every line boundary Python recognizes.
pub fn splitlines_keepends(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut iter = text.char_indices().peekable();
    while let Some((idx, ch)) = iter.next() {
        let end = match ch {
            '\r' => {
                if let Some(&(_, '\n')) = iter.peek() {
                    iter.next();
                    idx + 2
                } else {
                    idx + 1
                }
            }
            '\n' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}'
            | '\u{2029}' => idx + ch.len_utf8(),
            _ => continue,
        };
        out.push(&text[start..end]);
        start = end;
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

/// Python slice semantics for `seq[start:stop]` with optional bounds.
pub fn py_slice<T>(seq: &[T], start: Option<i64>, stop: Option<i64>) -> &[T] {
    let len = seq.len() as i64;
    let norm = |v: i64| -> i64 {
        if v < 0 {
            (len + v).max(0)
        } else {
            v.min(len)
        }
    };
    let s = start.map_or(0, norm);
    let e = stop.map_or(len, norm);
    if e <= s {
        &seq[0..0]
    } else {
        &seq[s as usize..e as usize]
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
struct Match {
    a: usize,
    b: usize,
    size: usize,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Tag {
    Replace,
    Delete,
    Insert,
    Equal,
}

type Opcode = (Tag, usize, usize, usize, usize);

struct SequenceMatcher<'a> {
    a: &'a [&'a str],
    b: &'a [&'a str],
    b2j: HashMap<&'a str, Vec<usize>>,
}

impl<'a> SequenceMatcher<'a> {
    fn new(a: &'a [&'a str], b: &'a [&'a str]) -> Self {
        let mut b2j: HashMap<&'a str, Vec<usize>> = HashMap::new();
        for (i, elt) in b.iter().enumerate() {
            b2j.entry(*elt).or_default().push(i);
        }
        let n = b.len();
        if n >= 200 {
            let ntest = n / 100 + 1;
            b2j.retain(|_, idxs| idxs.len() <= ntest);
        }
        Self { a, b, b2j }
    }

    fn find_longest_match(&self, alo: usize, ahi: usize, blo: usize, bhi: usize) -> Match {
        let (a, b) = (self.a, self.b);
        let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0usize);
        let mut j2len: HashMap<usize, usize> = HashMap::new();
        for (i, a_elt) in a.iter().enumerate().take(ahi).skip(alo) {
            let mut newj2len: HashMap<usize, usize> = HashMap::new();
            if let Some(idxs) = self.b2j.get(a_elt) {
                for &j in idxs {
                    if j < blo {
                        continue;
                    }
                    if j >= bhi {
                        break;
                    }
                    let prev = if j == 0 {
                        0
                    } else {
                        j2len.get(&(j - 1)).copied().unwrap_or(0)
                    };
                    let k = prev + 1;
                    newj2len.insert(j, k);
                    if k > bestsize {
                        besti = i + 1 - k;
                        bestj = j + 1 - k;
                        bestsize = k;
                    }
                }
            }
            j2len = newj2len;
        }
        // No junk elements exist (isjunk=None), so extension proceeds over any equal element,
        // including "popular" ones removed from b2j by autojunk.
        while besti > alo && bestj > blo && a[besti - 1] == b[bestj - 1] {
            besti -= 1;
            bestj -= 1;
            bestsize += 1;
        }
        while besti + bestsize < ahi
            && bestj + bestsize < bhi
            && a[besti + bestsize] == b[bestj + bestsize]
        {
            bestsize += 1;
        }
        Match {
            a: besti,
            b: bestj,
            size: bestsize,
        }
    }

    fn get_matching_blocks(&self) -> Vec<Match> {
        let (la, lb) = (self.a.len(), self.b.len());
        let mut queue = vec![(0usize, la, 0usize, lb)];
        let mut blocks: Vec<Match> = Vec::new();
        while let Some((alo, ahi, blo, bhi)) = queue.pop() {
            let x = self.find_longest_match(alo, ahi, blo, bhi);
            if x.size > 0 {
                blocks.push(x);
                if alo < x.a && blo < x.b {
                    queue.push((alo, x.a, blo, x.b));
                }
                if x.a + x.size < ahi && x.b + x.size < bhi {
                    queue.push((x.a + x.size, ahi, x.b + x.size, bhi));
                }
            }
        }
        blocks.sort();
        let (mut i1, mut j1, mut k1) = (0usize, 0usize, 0usize);
        let mut non_adjacent = Vec::new();
        for m in blocks {
            if i1 + k1 == m.a && j1 + k1 == m.b {
                k1 += m.size;
            } else {
                if k1 > 0 {
                    non_adjacent.push(Match {
                        a: i1,
                        b: j1,
                        size: k1,
                    });
                }
                i1 = m.a;
                j1 = m.b;
                k1 = m.size;
            }
        }
        if k1 > 0 {
            non_adjacent.push(Match {
                a: i1,
                b: j1,
                size: k1,
            });
        }
        non_adjacent.push(Match {
            a: la,
            b: lb,
            size: 0,
        });
        non_adjacent
    }

    fn get_opcodes(&self) -> Vec<Opcode> {
        let (mut i, mut j) = (0usize, 0usize);
        let mut answer = Vec::new();
        for m in self.get_matching_blocks() {
            let tag = if i < m.a && j < m.b {
                Some(Tag::Replace)
            } else if i < m.a {
                Some(Tag::Delete)
            } else if j < m.b {
                Some(Tag::Insert)
            } else {
                None
            };
            if let Some(t) = tag {
                answer.push((t, i, m.a, j, m.b));
            }
            i = m.a + m.size;
            j = m.b + m.size;
            if m.size > 0 {
                answer.push((Tag::Equal, m.a, i, m.b, j));
            }
        }
        answer
    }

    fn get_grouped_opcodes(&self, n: usize) -> Vec<Vec<Opcode>> {
        let mut codes = self.get_opcodes();
        if codes.is_empty() {
            codes.push((Tag::Equal, 0, 1, 0, 1));
        }
        if codes[0].0 == Tag::Equal {
            let (t, i1, i2, j1, j2) = codes[0];
            codes[0] = (
                t,
                i1.max(i2.saturating_sub(n)),
                i2,
                j1.max(j2.saturating_sub(n)),
                j2,
            );
        }
        let last = codes.len() - 1;
        if codes[last].0 == Tag::Equal {
            let (t, i1, i2, j1, j2) = codes[last];
            codes[last] = (t, i1, i2.min(i1 + n), j1, j2.min(j1 + n));
        }
        let nn = n + n;
        let mut groups = Vec::new();
        let mut group: Vec<Opcode> = Vec::new();
        for (tag, mut i1, i2, mut j1, j2) in codes {
            if tag == Tag::Equal && i2 - i1 > nn {
                group.push((tag, i1, i2.min(i1 + n), j1, j2.min(j1 + n)));
                groups.push(std::mem::take(&mut group));
                i1 = i1.max(i2.saturating_sub(n));
                j1 = j1.max(j2.saturating_sub(n));
            }
            group.push((tag, i1, i2, j1, j2));
        }
        if !group.is_empty() && !(group.len() == 1 && group[0].0 == Tag::Equal) {
            groups.push(group);
        }
        groups
    }
}

fn format_range_unified(start: usize, stop: usize) -> String {
    let mut beginning = start + 1;
    let length = stop - start;
    if length == 1 {
        return beginning.to_string();
    }
    if length == 0 {
        beginning -= 1;
    }
    format!("{beginning},{length}")
}

/// `difflib.unified_diff(a, b, fromfile, tofile, n=n)` as a list of yielded lines (`lineterm="\n"`).
pub fn unified_diff(a: &[&str], b: &[&str], fromfile: &str, tofile: &str, n: usize) -> Vec<String> {
    let sm = SequenceMatcher::new(a, b);
    let mut out = Vec::new();
    for group in sm.get_grouped_opcodes(n) {
        if out.is_empty() {
            out.push(format!("--- {fromfile}\n"));
            out.push(format!("+++ {tofile}\n"));
        }
        let first = group[0];
        let last = group[group.len() - 1];
        out.push(format!(
            "@@ -{} +{} @@\n",
            format_range_unified(first.1, last.2),
            format_range_unified(first.3, last.4)
        ));
        for (tag, i1, i2, j1, j2) in group {
            if tag == Tag::Equal {
                for line in &a[i1..i2] {
                    out.push(format!(" {line}"));
                }
                continue;
            }
            if matches!(tag, Tag::Replace | Tag::Delete) {
                for line in &a[i1..i2] {
                    out.push(format!("-{line}"));
                }
            }
            if matches!(tag, Tag::Replace | Tag::Insert) {
                for line in &b[j1..j2] {
                    out.push(format!("+{line}"));
                }
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splitlines_matches_python_boundaries() {
        assert_eq!(
            splitlines_keepends("a\nb\r\nc\rd\u{2028}e"),
            vec!["a\n", "b\r\n", "c\r", "d\u{2028}", "e"]
        );
        assert!(splitlines_keepends("").is_empty());
    }

    #[test]
    fn unified_diff_single_change() {
        let a = splitlines_keepends("1\n2\n3\n4\n5\n6\n7\n");
        let b = splitlines_keepends("1\n2\n3\nX\n5\n6\n7\n");
        let d = unified_diff(&a, &b, "a/f", "b/f", 2).concat();
        assert_eq!(
            d,
            "--- a/f\n+++ b/f\n@@ -2,5 +2,5 @@\n 2\n 3\n-4\n+X\n 5\n 6\n"
        );
    }

    #[test]
    fn identical_inputs_produce_nothing() {
        let a = splitlines_keepends("x\ny\n");
        assert!(unified_diff(&a, &a, "a", "b", 2).is_empty());
    }

    #[test]
    fn py_slice_handles_negative_and_overflow() {
        let v = [1, 2, 3, 4];
        assert_eq!(py_slice(&v, Some(1), Some(-1)), &[2, 3]);
        assert_eq!(py_slice(&v, Some(0), Some(99)), &[1, 2, 3, 4]);
        assert!(py_slice(&v, Some(3), Some(1)).is_empty());
    }
}

#[cfg(test)]
mod parity {
    use super::*;

    /// Cases generated by `scripts/gen_difflib_fixtures.py` from CPython's `difflib`.
    #[test]
    fn matches_cpython_difflib_reference_cases() {
        let raw = include_str!("../tests/fixtures/difflib_cases.json");
        let cases: Vec<serde_json::Value> = serde_json::from_str(raw).expect("fixture json");
        assert!(cases.len() >= 100);
        for (i, case) in cases.iter().enumerate() {
            let a = case["a"].as_str().unwrap();
            let b = case["b"].as_str().unwrap();
            let want = case["diff"].as_str().unwrap();
            let al = splitlines_keepends(a);
            let bl = splitlines_keepends(b);
            let got = unified_diff(&al, &bl, "a/f.py", "b/f.py", 2).concat();
            assert_eq!(got, want, "case {i} differs from CPython difflib");
        }
    }
}
