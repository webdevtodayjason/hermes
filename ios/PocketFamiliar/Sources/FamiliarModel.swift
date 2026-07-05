import Foundation
import SwiftUI

/// Observable mirror of the gateway's device state, built from live frames.
@MainActor
final class FamiliarModel: ObservableObject {
    struct FeedItem: Identifiable {
        let id = UUID()
        let role: String       // "u" | "a" | "!" (notify) | "·" (system)
        let text: String
        let at = Date()
    }
    struct DeckButton: Identifiable {
        let id: Int
        let label: String
        let color: String
        let confirm: Bool
    }
    struct Approval: Identifiable {
        let id: String
        let text: String
        let choices: [String]
    }
    struct Page {
        let title: String
        let lines: [String]
    }

    @Published var status: FamiliarClient.Status = .offline
    @Published var msg = ""
    @Published var total = 0
    @Published var running = 0
    @Published var waiting = 0
    @Published var tokensToday = 0
    @Published var toolsToday = 0
    @Published var jobState = "idle"
    @Published var jobLabel = ""
    @Published var feed: [FeedItem] = []
    @Published var deck: [DeckButton] = []
    @Published var approval: Approval?
    @Published var pages: [Int: Page] = [:]

    private let client = FamiliarClient()
    private var seededFeed = false

    // settings keys shared with @AppStorage in SettingsView; launch args
    // (-host x -token y) override via the NSArgumentDomain for sim testing
    static let defaultHost = "100.76.142.48"
    static let defaultPort = 8768

    init() {
        client.onStatus = { [weak self] s in self?.status = s }
        client.onFrame = { [weak self] f in self?.apply(f) }
    }

    func connectFromSettings() {
        let d = UserDefaults.standard
        let host = d.string(forKey: "host") ?? Self.defaultHost
        let port = d.object(forKey: "port") != nil ? d.integer(forKey: "port") : Self.defaultPort
        client.connect(host: host, port: port == 0 ? Self.defaultPort : port,
                       token: d.string(forKey: "token") ?? "")
    }

    func apply(_ f: [String: Any]) {
        switch f["type"] as? String {
        case "state":
            total = f["total"] as? Int ?? total
            running = f["running"] as? Int ?? running
            waiting = f["waiting"] as? Int ?? waiting
            msg = f["msg"] as? String ?? msg
            tokensToday = f["tokens_today"] as? Int ?? tokensToday
            toolsToday = f["tools_today"] as? Int ?? toolsToday
            jobState = f["job_state"] as? String ?? jobState
            jobLabel = f["job_label"] as? String ?? jobLabel
            if waiting == 0 { approval = nil }   // resolved elsewhere (desk / CLI)
            if !seededFeed, let entries = f["entries"] as? [String], !entries.isEmpty {
                seededFeed = true
                feed = entries.map { line in
                    let (role, text) = Self.splitTicker(line)
                    return FeedItem(role: role, text: text)
                }
            }
        case "event":
            if f["event"] as? String == "message", let m = f["msg"] as? String {
                append(role: (f["role"] as? String)?.hasPrefix("u") == true ? "u" : "a", text: m)
            }
        case "notify":
            if let m = f["msg"] as? String { append(role: "!", text: m) }
        case "deck":
            if let buttons = f["buttons"] as? [[String: Any]] {
                deck = buttons.map {
                    DeckButton(id: $0["i"] as? Int ?? 0,
                               label: $0["label"] as? String ?? "?",
                               color: $0["color"] as? String ?? "green",
                               confirm: $0["confirm"] as? Bool ?? false)
                }
            }
        case "permission":
            approval = Approval(id: "\(f["id"] ?? "")",
                                text: f["text"] as? String ?? "approval pending",
                                choices: (f["choices"] as? [String]) ?? ["once", "deny"])
        case "page":
            if let slot = f["slot"] as? Int {
                pages[slot] = Page(title: f["title"] as? String ?? "",
                                   lines: f["lines"] as? [String] ?? [])
            }
        case "ack":
            if let m = f["msg"] as? String { append(role: "·", text: m) }
        default:
            break
        }
    }

    // MARK: M2 — device→host frames (identical to the desk device's taps)

    func runDeck(_ b: DeckButton) {
        client.send(["cmd": "deck", "i": b.id])
    }

    func resolveApproval(_ decision: String) {
        guard let a = approval else { return }
        client.send(["cmd": "permission", "decision": decision, "id": a.id])
        approval = nil   // optimistic — plugin acks and the next state frame confirms
    }

    func jobAction(_ action: String) {
        client.send(["cmd": "action", "action": action])
    }

    private func append(role: String, text: String) {
        feed.append(FeedItem(role: role, text: text))
        if feed.count > 300 { feed.removeFirst(feed.count - 300) }
    }

    static func splitTicker(_ line: String) -> (String, String) {
        if line.hasPrefix("u:") { return ("u", String(line.dropFirst(2)).trimmingCharacters(in: .whitespaces)) }
        if line.hasPrefix("a:") { return ("a", String(line.dropFirst(2)).trimmingCharacters(in: .whitespaces)) }
        return ("·", line)
    }
}
