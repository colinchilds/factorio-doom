//! fsim must reproduce the game's recorded traces exactly (golden dumps from harness/run.py).

use std::path::Path;

fn check(cases: &str, dump: &str) {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let results = fsim::conform::run(&root.join(cases), &root.join(dump), None).expect("conformance run");
    assert!(!results.is_empty(), "no cases found in {cases}");
    let failures: Vec<String> = results
        .iter()
        .filter(|r| !r.mismatches.is_empty())
        .map(|r| format!("{}: {}", r.name, r.mismatches.iter().take(3).cloned().collect::<Vec<_>>().join("\n")))
        .collect();
    assert!(failures.is_empty(), "fsim differs from the game:\n{}", failures.join("\n"));
}

#[test]
fn conformance_cases_match_game() {
    check("conformance/cases", "conformance/game_dump.json");
}

#[test]
fn gradient_demo_matches_game() {
    check("demos", "demos/golden/gradient_dump.json");
}
