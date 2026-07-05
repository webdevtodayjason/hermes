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
    private var generation = 0          // invalidates stale callbacks after reconfigure
    private var lastRx = Date()
    private var watchdog: Timer?

    override init() {
        super.init()
        session = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
    }

    func connect(host: String, port: Int, token: String) {
        let trimmed = host.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty, let u = URL(string: "ws://\(trimmed):\(port)") else { return }
        url = u
        self.token = token
        generation += 1
        task?.cancel(with: .goingAway, reason: nil)
        open(generation)
        DispatchQueue.main.async { [self] in
            watchdog?.invalidate()
            // server heartbeats every ~2s; silence means the link is dead
            watchdog = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
                guard let self, let t = self.task else { return }
                if Date().timeIntervalSince(self.lastRx) > 12 {
                    t.cancel(with: .goingAway, reason: nil)   // receive() fails -> retry path
                }
            }
        }
    }

    func send(_ obj: [String: Any]) {
        guard let task, let data = try? JSONSerialization.data(withJSONObject: obj),
              let text = String(data: data, encoding: .utf8) else { return }
        task.send(.string(text)) { _ in }
    }

    private func open(_ gen: Int) {
        guard let url else { return }
        status(.connecting)
        let t = session.webSocketTask(with: url)
        task = t
        t.resume()
        receive(gen)
    }

    private func retry(_ gen: Int) {
        guard gen == generation else { return }
        status(.offline)
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
            guard let self, gen == self.generation else { return }
            self.open(gen)
        }
    }

    private func receive(_ gen: Int) {
        task?.receive { [weak self] result in
            guard let self, gen == self.generation else { return }
            switch result {
            case .failure:
                self.retry(gen)
            case .success(let message):
                self.lastRx = Date()
                if case .string(let text) = message { self.handle(text) }
                if case .data(let data) = message,
                   let text = String(data: data, encoding: .utf8) { self.handle(text) }
                self.receive(gen)
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
        send(["type": "auth", "token": token])
    }
}
