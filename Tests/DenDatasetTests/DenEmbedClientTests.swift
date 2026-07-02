import XCTest
@testable import DenDataset

/// FP-2 — the den-embed client. Decoding is pinned by a fixture; the HTTP round-trip is exercised against a
/// `URLProtocol` stub (deterministic, offline) that asserts the request POSTs the doc in a JSON body (not a
/// GET ?text= query param — a long plot would 414). If a real den-embed service is reachable, one live probe
/// additionally checks the 1024-dim int8 contract.
final class DenEmbedClientTests: XCTestCase {
    func testDecodeReturnsInt8Vector() throws {
        let json = #"{"vector":[1,-2,127,-127,0],"dims":5,"model":"bge-m3"}"#
        XCTAssertEqual(try DenEmbedClient.decode(Data(json.utf8)), [1, -2, 127, -127, 0])
    }

    func testEmbedInt8HitsServiceAndDecodes() async throws {
        StubURLProtocol.handler = { request in
            let url = try XCTUnwrap(request.url)
            XCTAssertTrue(url.path.hasSuffix("/embed"), "POSTs /embed")
            XCTAssertEqual(request.httpMethod, "POST", "the doc is POSTed, not a GET ?text=")
            let sent = Self.requestBody(request).flatMap { try? JSONDecoder().decode([String: String].self, from: $0) }
            XCTAssertEqual(sent?["text"], "hello world", "the doc is in the JSON body")
            let body = #"{"vector":[10,20,-30],"dims":3,"model":"bge-m3"}"#
            let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (response, Data(body.utf8))
        }
        defer { StubURLProtocol.handler = nil }

        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubURLProtocol.self]
        let client = DenEmbedClient(baseURL: URL(string: "http://localhost:8791")!,
                                    session: URLSession(configuration: config))
        let vector = try await client.embedInt8("hello world")
        XCTAssertEqual(vector, [10, 20, -30])
    }

    /// Opt-in live probe — only runs when a den-embed service is actually up (skipped otherwise so CI stays
    /// hermetic). Verifies the real 1024-dim int8 contract end-to-end.
    func testLiveServiceContractIfReachable() async throws {
        guard let base = Self.reachableService() else {
            throw XCTSkip("no den-embed service reachable (set DEN_EMBED_URL or boot it) — skipping live probe")
        }
        let vector = try await DenEmbedClient(baseURL: base).embedInt8("A neo-noir detective thriller.")
        XCTAssertEqual(vector.count, 1024, "bge-m3 is 1024-dim")
        XCTAssertTrue(vector.allSatisfy { $0 >= -127 && $0 <= 127 }, "int8 stays in [-127, 127]")
    }

    /// URLSession converts a request's `httpBody` into an `httpBodyStream` by the time the stub sees it, so read
    /// whichever is present.
    static func requestBody(_ request: URLRequest) -> Data? {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open(); defer { stream.close() }
        var data = Data(); var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: buffer.count)
            if read <= 0 { break }
            data.append(buffer, count: read)
        }
        return data
    }

    private static func reachableService() -> URL? {
        let base = DenEmbedClient.defaultBaseURL()
        let health = base.appendingPathComponent("health")
        var ok = false
        let sema = DispatchSemaphore(value: 0)
        var request = URLRequest(url: health)
        request.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: request) { _, response, _ in
            if let http = response as? HTTPURLResponse, http.statusCode == 200 { ok = true }
            sema.signal()
        }.resume()
        _ = sema.wait(timeout: .now() + 2)
        return ok ? base : nil
    }
}

/// A minimal in-process HTTP stub — returns a canned response for any request on the session it's installed on.
final class StubURLProtocol: URLProtocol {
    nonisolated(unsafe) static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse)); return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}
