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
/// `embed-corpus` is a Python stage now; the reader it exposed still parses `facts`, so these drive that.
final class ArgsTests: XCTestCase {
    func testUnknownFlagIsRefused() throws {
        let result = run(["facts", "--out-dir", "out", "--has-vectr"])
        XCTAssertNotEqual(result.status, 0, "an unrecognised flag must not be accepted")
        XCTAssert(result.stderr.contains("unknown flag --has-vectr"),
                  "the refusal names the flag it refused: \(result.stderr)")
    }

    func testANearMissNamesTheFlagItWasMeantToBe() throws {
        let result = run(["facts", "--out-dir", "out", "--has-vectr"])
        XCTAssert(result.stderr.contains("Did you mean --has-vector?"),
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
        let result = run(["facts", "--out-dir", "out", "--batch", "--has-vector"])
        XCTAssertNotEqual(result.status, 0, "a value flag with no value must not be accepted")
        XCTAssert(result.stderr.contains("--batch") && result.stderr.contains("takes a value"),
                  "the refusal names the flag left without a value: \(result.stderr)")
    }

    func testMissingRequiredFlagIsRefused() throws {
        let result = run(["facts", "--labels", "labels.json"])
        XCTAssertNotEqual(result.status, 0)
        XCTAssert(result.stderr.contains("missing required --out-dir"), result.stderr)
    }

    func testABareFlagAndAValueFlagBothStillParse() throws {
        // A bare flag sitting BETWEEN a value flag and a required one is where the two shapes can go wrong
        // without saying so. If `--has-vector` consumed the next token the way a value flag does, it would
        // swallow `--labels` and the run would read no labels; if `--batch` did not take its 5, the 5 would
        // be refused as an unknown flag. Reaching the labels FILE — which is read before anything is asked
        // of Wikidata — is what proves neither happened.
        let result = run(["facts", "--out-dir", "/nonexistent", "--batch", "5",
                          "--has-vector", "--labels", "/nonexistent/labels.json"])
        XCTAssertNotEqual(result.status, 0)
        XCTAssertFalse(result.stderr.contains("missing required"),
                       "the bare flag consumed its neighbour: \(result.stderr)")
        XCTAssertFalse(result.stderr.contains("unknown flag"),
                       "the value flag did not take its value: \(result.stderr)")
        XCTAssert(result.stderr.contains("labels.json"),
                  "both flags reached the command body: \(result.stderr)")
    }

    func testHelpListsTheSubcommandsFlags() throws {
        let result = run(["facts", "--help"])
        XCTAssertEqual(result.status, 0, "--help is not an error")
        for flag in ["--out-dir", "--ids", "--labels", "--batch", "--has-vector", "--titles-only"] {
            XCTAssert(result.stdout.contains(flag), "\(flag) is missing from facts --help")
        }
        XCTAssert(result.stdout.contains("(required)"), "required flags are marked as such")
        XCTAssertFalse(result.stdout.contains("--vectors"), "another command's flags are not listed")
    }

    func testABareInvocationListsTheSubcommands() throws {
        let result = run([])
        XCTAssertNotEqual(result.status, 0, "naming no command is a usage error")
        // Every command that survives: the vote-pass generation went with the Jev pass, the deterministic
        // ones — `finalize` and `embed-corpus` among them — and `enrich` are Python now, and `metadata` went
        // with the poster sidecar. Naming all of them rather than a sample, so a command that disappears from
        // the overview fails here rather than in an operator's terminal.
        for command in ["facts", "recluster"] {
            XCTAssert(result.stderr.contains(command), "\(command) is missing from the overview")
        }
    }

    func testAnUnknownSubcommandListsTheSubcommands() throws {
        let result = run(["embed-korpus"])
        XCTAssertNotEqual(result.status, 0)
        XCTAssert(result.stderr.contains("unknown command 'embed-korpus'"), result.stderr)
        XCTAssert(result.stderr.contains("facts"), "the overview follows the refusal")
    }

    // MARK: - helpers

    private struct Result {
        let status: Int32
        let stdout: String
        let stderr: String
    }

    /// Run the tool and capture both streams. A non-zero exit is the expected outcome of most of these, so
    /// the status is returned rather than failed on.
    private func run(_ arguments: [String]) -> Result {
        let process = Process()
        process.executableURL = Self.binaryURL
        process.arguments = arguments
        let out = Pipe(), err = Pipe()
        process.standardOutput = out
        process.standardError = err
        do { try process.run() } catch {
            XCTFail("could not run \(Self.binaryURL.path): \(error)")
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

    /// The `taxonomy-backfill` executable sits in the same products directory as the xctest bundle.
    static let binaryURL: URL = {
        for bundle in Bundle.allBundles where bundle.bundlePath.hasSuffix(".xctest") {
            return bundle.bundleURL.deletingLastPathComponent().appendingPathComponent("taxonomy-backfill")
        }
        // Linux / plain builds: the test runner sits beside the executable.
        return Bundle.main.bundleURL.appendingPathComponent("taxonomy-backfill")
    }()
}
