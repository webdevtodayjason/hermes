import Foundation

/// WebSocket leg of the familiar protocol: connect, auth-first-frame,
/// then newline-JSON frames in both directions (same frames as USB serial).
/// Reconnects forever on a fixed delay. // ponytail: fixed 3s retry, backoff if it ever thrashes
final class FamiliarClient: NSObject, URLSessionWebSocketDelegate {
    enum Status: String { case offline, connecting, live }

    var onFrame: (([String: Any]) -> Void)?
    var onStatus: ((Status) -> Void)?

    private var session: URLSession!
    private var task: URLSessionWebSocketTask?
    private var url: URL?
    private var token = ""
    // bumped on every open; a receive loop or queued retry from an older
    // attempt sees the mismatch and dies. Exactly one live loop per socket —
    // URLSessionWebSocketTask allows only ONE pending receive, so a second
    // loop on the same task errors the whole connection.
    private var attempt = 0
    private var lastRx = Date()
    private var watchdog: Timer?

    override init() {
        super.init()
        // main-queue delegate: all client state is touched from one queue
        session = URLSession(configuration: .default, delegate: self,
                             delegateQueue: OperationQueue.main)
    }

    func connect(host: String, port: Int, token: String) {
        let trimmed = host.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty, let u = URL(string: "ws://\(trimmed):\(port)") else { return }
        url = u
        self.token = token
        DispatchQueue.main.async { [self] in
            open()
            watchdog?.invalidate()
            // server heartbeats every ~2s; silence means the link is dead
            watchdog = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
                guard let self, let t = self.task else { return }
                if Date().timeIntervalSince(self.lastRx) > 12 {
                    t.cancel(with: .goingAway, reason: nil)   // receive fails -> retry path
                }
            }
        }
    }

    func send(_ obj: [String: Any]) {
        guard let task, let data = try? JSONSerialization.data(withJSONObject: obj),
              let text = String(data: data, encoding: .utf8) else { return }
        task.send(.string(text)) { _ in }
    }

    private func open() {
        guard let url else { return }
        status(.connecting)
        attempt += 1
        lastRx = Date()
        task?.cancel(with: .goingAway, reason: nil)
        let t = session.webSocketTask(with: url)
        task = t
        t.resume()
        receive(t, attempt)
    }

    private func scheduleRetry() {
        status(.offline)
        let a = attempt
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
            guard let self, a == self.attempt else { return }  // superseded meanwhile
            self.open()
        }
    }

    private func receive(_ t: URLSessionWebSocketTask, _ a: Int) {
        t.receive { [weak self] result in
            guard let self, a == self.attempt else { return }  // stale loop dies here
            switch result {
            case .failure:
                self.scheduleRetry()
            case .success(let message):
                self.lastRx = Date()
                if case .string(let text) = message { self.handle(text) }
                if case .data(let data) = message,
                   let text = String(data: data, encoding: .utf8) { self.handle(text) }
                self.receive(t, a)
            }
        }
    }

    private func handle(_ text: String) {
        // tolerate several newline-joined frames in one WS message
        for line in text.split(separator: "\n") {
            guard let data = line.data(using: .utf8),
                  let obj = try? JSONSerialization.jsonObject(with: data),
                  let dict = obj as? [String: Any] else { continue }
            if dict["type"] as? String == "auth", dict["ok"] as? Bool == true {
                status(.live)
                continue
            }
            DispatchQueue.main.async { self.onFrame?(dict) }
        }
    }

    private func status(_ s: Status) {
        DispatchQueue.main.async { self.onStatus?(s) }
    }

    // MARK: URLSessionWebSocketDelegate
    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol protocol: String?) {
        // auth on the task that opened — not self.task, which may be newer
        guard let data = try? JSONSerialization.data(withJSONObject: ["type": "auth", "token": token]),
              let text = String(data: data, encoding: .utf8) else { return }
        webSocketTask.send(.string(text)) { _ in }
    }
}
