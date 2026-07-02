import Foundation

/// One user/assistant turn of an LLM conversation.
public struct LLMMessage: Sendable, Equatable {
    public enum Role: String, Sendable { case user, assistant }
    public let role: Role
    public let content: String
    public init(role: Role, content: String) { self.role = role; self.content = content }
    public static func user(_ text: String) -> LLMMessage { .init(role: .user, content: text) }
    public static func assistant(_ text: String) -> LLMMessage { .init(role: .assistant, content: text) }
}

/// A provider-agnostic completion request. The producer builds one per title in `TaxonomyClassifier` even
/// though the shipped path never sends it (the `NoLLM` stub satisfies the initializer); the votes come from
/// Haiku subagents driven by the CLI, not an in-process API call.
public struct LLMRequest: Sendable {
    public var system: String?
    public var messages: [LLMMessage]
    public var maxTokens: Int
    public var temperature: Double

    public init(system: String? = nil, messages: [LLMMessage], maxTokens: Int = 1024, temperature: Double = 0.2) {
        self.system = system
        self.messages = messages
        self.maxTokens = maxTokens
        self.temperature = temperature
    }

    public init(system: String? = nil, user: String, maxTokens: Int = 1024, temperature: Double = 0.2) {
        self.init(system: system, messages: [.user(user)], maxTokens: maxTokens, temperature: temperature)
    }
}

/// Provider-agnostic text completion. In the producer only the `NoLLM` stub conforms — the calibrated
/// aggregation seam (`TaxonomyClassifier.classify(rawVotes:)`) never calls it.
public protocol LLMClient: Sendable {
    func complete(_ request: LLMRequest) async throws -> String
}
