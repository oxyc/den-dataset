import XCTest
@testable import DenDataset

/// The canary is a hard gate on every vector this repo writes, so the comparison and the two digests are
/// tested without a service. A gate that silently passed would look exactly like a healthy run.
final class EmbedSpaceCanaryTests: XCTestCase {
    /// `data/embed-canary.json` — the committed answers, reached from this file rather than from the
    /// working directory, which `swift test` does not fix.
    private var canaryPath: String {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // DenDatasetTests
            .deletingLastPathComponent()   // Tests
            .deletingLastPathComponent()   // repo root
            .appendingPathComponent("data/embed-canary.json").path
    }

    private func identical(_ n: Int) -> [Int8] { (0..<n).map { Int8(truncatingIfNeeded: $0 * 7 - 63) } }

    func testIdenticalVectorsPass() {
        let v = identical(1024)
        let result = EmbedSpaceCanary.compare(id: "x", expected: v, actual: v)
        XCTAssertTrue(result.ok)
        XCTAssertEqual(result.dimsDiffering, 0)
        XCTAssertEqual(result.maxAbsDelta, 0)
        XCTAssertEqual(result.cosine, 1.0, accuracy: 1e-12)
    }

    /// The bar is byte-identical, not "close". One dimension off by one is the smallest possible
    /// difference and it still fails — cosine rounds to 1.000000 there, which is exactly why cosine is
    /// reported and never consulted.
    func testOneDimensionOffByOneFails() {
        var actual = identical(1024)
        actual[500] = actual[500] &+ 1
        let result = EmbedSpaceCanary.compare(id: "x", expected: identical(1024), actual: actual)
        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.dimsDiffering, 1)
        XCTAssertEqual(result.maxAbsDelta, 1)
        XCTAssertGreaterThan(result.cosine, 0.9999)
    }

    /// A length difference is not a near miss. Nothing is comparable dimension by dimension, so the
    /// result must not report a small number of differing dims and a plausible cosine.
    func testDifferentLengthFails() {
        let result = EmbedSpaceCanary.compare(id: "x", expected: identical(1024), actual: identical(384))
        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.dimsDiffering, 1024)
        XCTAssertEqual(result.cosine, 0)
    }

    /// The committed file's own claims recompute. This is what stops a hand-edited canary — a vector
    /// pasted in, a digest corrected by hand — from being published as the name of a space.
    func testCommittedCanaryIsSelfConsistent() throws {
        let file = try EmbedSpaceCanary.File.read(canaryPath)
        XCTAssertEqual(EmbedSpaceCanary.textsSha256(file.cases), file.textsSha256)
        XCTAssertEqual(EmbedSpaceCanary.spaceId(canarySet: file.canarySet, cases: file.cases),
                       file.spaceId)
        XCTAssertTrue(file.spaceId.hasPrefix(file.canarySet + ":"),
                      "the set name is carried IN the id so two sets cannot be compared by accident")
        for item in file.cases {
            XCTAssertEqual(item.vector?.count, file.dims, "case \(item.id) is not \(file.dims)-dim")
        }
    }

    /// Both digests are computed twice in this repo — here and in `scripts/v2/embed_canary.py` — because
    /// the corpus is written by both a Swift and a Python path. They are pinned against the committed
    /// file, which the Python side produced, so the two implementations cannot drift apart into agreeing
    /// on nothing.
    func testTheTwoImplementationsAgreeOnTheCommittedDigests() throws {
        let file = try EmbedSpaceCanary.File.read(canaryPath)
        XCTAssertEqual(file.spaceId,
                       "canary-v1:42f4618a055103411edec2ad0f87dfa94f1e7f3d40bf768563e82a0fe692a2df")
        XCTAssertEqual(file.textsSha256,
                       "fe8255efc91c808177f7ec92fb004edbee7afd2b79dbc6e94fc581d187026f96")
    }

    /// The texts are the instrument and are deliberately outside `spaceId`, so a reworded probe does not
    /// rename a space it did not change. `textsSha256` is what notices that edit.
    func testEditingATextMovesOnlyTheTextDigest() throws {
        let file = try EmbedSpaceCanary.File.read(canaryPath)
        var edited = file.cases
        edited[0] = EmbedSpaceCanary.Case(id: edited[0].id, why: edited[0].why,
                                          text: edited[0].text + " and one more clause", v: edited[0].v)
        XCTAssertEqual(EmbedSpaceCanary.spaceId(canarySet: file.canarySet, cases: edited), file.spaceId)
        XCTAssertNotEqual(EmbedSpaceCanary.textsSha256(edited), file.textsSha256)
    }

    /// One byte of one vector renames the space. That is the property the manifest's `embeddingSpace`
    /// rests on.
    func testOneChangedVectorByteMovesTheSpaceId() throws {
        let file = try EmbedSpaceCanary.File.read(canaryPath)
        var bytes = Array(Data(base64Encoded: file.cases[0].v)!)
        bytes[0] = bytes[0] &+ 1
        var edited = file.cases
        edited[0] = EmbedSpaceCanary.Case(id: edited[0].id, why: edited[0].why, text: edited[0].text,
                                          v: Data(bytes).base64EncodedString())
        XCTAssertNotEqual(EmbedSpaceCanary.spaceId(canarySet: file.canarySet, cases: edited), file.spaceId)
    }

    /// `canarySet` is in the digest, so renaming the set renames every space measured with it — which is
    /// what makes "same set, different id" mean "different space" and nothing else.
    func testTheSetNameIsPartOfTheIdentity() throws {
        let file = try EmbedSpaceCanary.File.read(canaryPath)
        XCTAssertNotEqual(EmbedSpaceCanary.spaceId(canarySet: "canary-v2", cases: file.cases),
                          file.spaceId)
    }
}
