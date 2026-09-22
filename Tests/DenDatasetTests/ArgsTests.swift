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
/// `embed-corpus` and `facts` are Python stages now; the reader they exposed still parses `recluster`, so
/// these drive that.
final class ArgsTests: XCTestCase {
    private let required = ["--labels", "labels.json", "--vectors", "vectors.bin", "--out", "report.json"]

    func testUnknownFlagIsRefused() throws {
        let result = run(["recluster"] + required + ["--min-cohesoin", "0.5"])
        XCTAssertNotEqual(result.status, 0, "an unrecognised flag must not be accepted")
        XCTAssert(result.stderr.contains("unknown flag --min-cohesoin"),
                  "the refusal names the flag it refused: \(result.stderr)")
    }

    func testANearMissNamesTheFlagItWasMeantToBe() throws {
        let result = run(["recluster"] + required + ["--min-cohesoin", "0.5"])
        XCTAssert(result.stderr.contains("Did you mean --min-cohesion?"),
                  "a one-character slip is named, which is what turns it into a one-line fix: \(result.stderr)")
    }

    func testAFarMissSuggestsNothing() throws {
        // The suggestion is bounded, so a flag that resembles nothing declared does not get pointed at an
        // unrelated one — a wrong suggestion is worse than none.
        let result = run(["recluster"] + required + ["--zzzzzzzzzz"])
        XCTAssertNotEqual(result.status, 0)
        XCTAssertFalse(result.stderr.contains("Did you mean"), result.stderr)
    }

    func testValueFlagWithNoValueIsRefused() throws {
        // The second silent path: `--plot-cap` became a BARE flag because the next token started with `--`,
        // so the cap fell back to 1500 while `--chunk` got the 7.
        let result = run(["recluster"] + required + ["--k", "--iterations", "5"])
        XCTAssertNotEqual(result.status, 0, "a value flag with no value must not be accepted")
        XCTAssert(result.stderr.contains("--k") && result.stderr.contains("takes a value"),
                  "the refusal names the flag left without a value: \(result.stderr)")
    }

    func testMissingRequiredFlagIsRefused() throws {
        let result = run(["recluster", "--labels", "labels.json", "--out", "report.json"])
        XCTAssertNotEqual(result.status, 0)
        XCTAssert(result.stderr.contains("missing required --vectors"), result.stderr)
    }

    func testHelpListsTheSubcommandsFlags() throws {
        let result = run(["recluster", "--help"])
        XCTAssertEqual(result.status, 0, "--help is not an error")
        for flag in ["--labels", "--vectors", "--out", "--k", "--iterations", "--min-size", "--max-purity",
                     "--min-cohesion"] {
            XCTAssert(result.stdout.contains(flag), "\(flag) is missing from recluster --help")
        }
        XCTAssert(result.stdout.contains("(required)"), "required flags are marked as such")
    }

    func testABareInvocationListsTheSubcommands() throws {
        let result = run([])
        XCTAssertNotEqual(result.status, 0, "naming no command is a usage error")
        // Every command that survives: the vote-pass generation went with the Jev pass, the deterministic
        // ones — `finalize`, `embed-corpus` and `facts` among them — and `enrich` are Python now, and
        // `metadata` went with the poster sidecar.
        XCTAssert(result.stderr.contains("recluster"), "recluster is missing from the overview")
    }

    func testAnUnknownSubcommandListsTheSubcommands() throws {
        let result = run(["embed-korpus"])
        XCTAssertNotEqual(result.status, 0)
        XCTAssert(result.stderr.contains("unknown command 'embed-korpus'"), result.stderr)
        XCTAssert(result.stderr.contains("recluster"), "the overview follows the refusal")
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
