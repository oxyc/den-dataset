import XCTest
@testable import DenDataset

/// The argument reader, driven through the real CLI binary.
///
/// `Args` and the per-subcommand flag tables live in the executable target, which nothing can import, so
/// these run the tool the way an operator does — which is also the only way to prove the refusals reach the
/// exit code rather than being caught somewhere on the way out.
///
/// The bug this exists for: the previous reader kept any `--token` it did not recognise and never read it.
/// A misspelled `--doc-drop-director` on `embed-corpus` left `dropDirector` false and exited 0, composing a
/// DIFFERENT embedding document — and on a fresh out-dir that wrong shape was then written into
/// index/composition.json as the store's recorded truth, which every later run was matched against. The
/// documents embed, the vectors rank, the neighbours look plausible; nothing downstream can detect it.
final class ArgsTests: XCTestCase {
    func testUnknownFlagIsRefused() throws {
        let result = run(["embed-corpus", "--labels", "labels.json", "--out-dir", "out",
                          "--doc-drop-directer"])
        XCTAssertNotEqual(result.status, 0, "an unrecognised flag must not be accepted")
        XCTAssert(result.stderr.contains("unknown flag --doc-drop-directer"),
                  "the refusal names the flag it refused: \(result.stderr)")
    }

    func testANearMissNamesTheFlagItWasMeantToBe() throws {
        let result = run(["embed-corpus", "--labels", "labels.json", "--out-dir", "out",
                          "--doc-drop-directer"])
        XCTAssert(result.stderr.contains("Did you mean --doc-drop-director?"),
                  "a one-character slip is named, which is what turns it into a one-line fix: \(result.stderr)")
    }

    func testAFarMissSuggestsNothing() throws {
        // The suggestion is bounded, so a flag that resembles nothing declared does not get pointed at an
        // unrelated one — a wrong suggestion is worse than none.
        let result = run(["recluster", "--labels", "labels.json", "--vectors", "vectors.bin",
                          "--out", "report.json", "--zzzzzzzzzz"])
        XCTAssertNotEqual(result.status, 0)
        XCTAssertFalse(result.stderr.contains("Did you mean"), result.stderr)
    }

    func testValueFlagWithNoValueIsRefused() throws {
        // The second silent path: `--plot-cap` became a BARE flag because the next token started with `--`,
        // so the cap fell back to 1500 while `--chunk` got the 7.
        let result = run(["embed-corpus", "--labels", "labels.json", "--out-dir", "out",
                          "--plot-cap", "--chunk", "7"])
        XCTAssertNotEqual(result.status, 0, "a value flag with no value must not be accepted")
        XCTAssert(result.stderr.contains("--plot-cap") && result.stderr.contains("takes a value"),
                  "the refusal names the flag left without a value: \(result.stderr)")
    }

    func testMissingRequiredFlagIsRefused() throws {
        let result = run(["enrich", "--limit", "5"])
        XCTAssertNotEqual(result.status, 0)
        XCTAssert(result.stderr.contains("missing required --worklist"), result.stderr)
    }

    func testABareFlagAndAValueFlagBothStillParse() throws {
        // A bare flag sitting BETWEEN a value flag and a required one is where the two shapes can go wrong
        // without saying so. If `--exclude-anime` consumed the next token the way a value flag does, it
        // would swallow `--worklist` and the run would die on a missing required flag; if `--limit` did not
        // take its 5, the 5 would be refused as an unknown flag. Reaching the worklist FILE is what proves
        // neither happened.
        let result = run(["enrich", "--out-dir", "/nonexistent", "--limit", "5",
                          "--exclude-anime", "--worklist", "/nonexistent/worklist.json"])
        XCTAssertNotEqual(result.status, 0)
        XCTAssertFalse(result.stderr.contains("missing required"),
                       "the bare flag consumed its neighbour: \(result.stderr)")
        XCTAssertFalse(result.stderr.contains("unknown flag"),
                       "the value flag did not take its value: \(result.stderr)")
        XCTAssert(result.stderr.contains("worklist.json"),
                  "both flags reached the command body: \(result.stderr)")
    }

    func testHelpListsTheSubcommandsFlags() throws {
        let result = run(["embed-corpus", "--help"])
        XCTAssertEqual(result.status, 0, "--help is not an error")
        for flag in ["--out-dir", "--labels", "--enriched-dir", "--doc-facts", "--doc-drop-director",
                     "--chunk", "--plot-cap", "--limit", "--pause-ms", "--dump-docs"] {
            XCTAssert(result.stdout.contains(flag), "\(flag) is missing from embed-corpus --help")
        }
        XCTAssert(result.stdout.contains("(required)"), "required flags are marked as such")
        XCTAssertFalse(result.stdout.contains("--vectors"), "another command's flags are not listed")
    }

    func testABareInvocationListsTheSubcommands() throws {
        let result = run([])
        XCTAssertNotEqual(result.status, 0, "naming no command is a usage error")
        // Every command that survives: the vote-pass generation went with the Jev pass, and the four
        // deterministic ones are Python stages now. Naming all five rather than a sample, so a command
        // that disappears from the overview fails here rather than in an operator's terminal.
        for command in ["enrich", "embed-corpus", "facts", "finalize", "recluster"] {
            XCTAssert(result.stderr.contains(command), "\(command) is missing from the overview")
        }
    }

    func testAnUnknownSubcommandListsTheSubcommands() throws {
        let result = run(["embed-korpus"])
        XCTAssertNotEqual(result.status, 0)
        XCTAssert(result.stderr.contains("unknown command 'embed-korpus'"), result.stderr)
        XCTAssert(result.stderr.contains("embed-corpus"), "the overview follows the refusal")
    }

    // MARK: - helpers

    private struct Result {
        let status: Int32
        let stdout: String
        let stderr: String
    }

    /// Run the tool and capture both streams. Unlike `SmokeTests.run`, a non-zero exit is the expected
    /// outcome of most of these, so the status is returned rather than failed on.
    private func run(_ arguments: [String]) -> Result {
        let process = Process()
        // The same binary SmokeTests drives, located once: a second copy of "where the products directory
        // is" is a thing to keep in step for no benefit.
        process.executableURL = SmokeTests.binaryURL
        process.arguments = arguments
        let out = Pipe(), err = Pipe()
        process.standardOutput = out
        process.standardError = err
        do { try process.run() } catch {
            XCTFail("could not run \(SmokeTests.binaryURL.path): \(error)")
            return Result(status: -1, stdout: "", stderr: "")
        }
        // Drained before waiting: a full pipe buffer would block the child forever.
        let outData = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return Result(status: process.terminationStatus,
                      stdout: String(data: outData, encoding: .utf8) ?? "",
                      stderr: String(data: errData, encoding: .utf8) ?? "")
    }
}
