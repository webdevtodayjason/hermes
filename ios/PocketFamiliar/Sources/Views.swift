import SwiftUI

// Phosphor terminal palette — same skin as the desk device.
enum Phosphor {
    static let green = Color(red: 0.20, green: 1.00, blue: 0.40)
    static let dim = Color(red: 0.10, green: 0.45, blue: 0.20)
    static let amber = Color(red: 1.00, green: 0.75, blue: 0.20)
    static let red = Color(red: 1.00, green: 0.30, blue: 0.25)
    static let cyan = Color(red: 0.30, green: 0.90, blue: 1.00)
    static let bg = Color(red: 0.02, green: 0.05, blue: 0.03)
}

struct ContentView: View {
    @EnvironmentObject var model: FamiliarModel
    // "-tab feed" launch arg opens a specific tab (headless sim verification)
    @State private var tab = UserDefaults.standard.string(forKey: "tab") ?? "face"

    var body: some View {
        TabView(selection: $tab) {
            FaceView().tabItem { Label("Face", systemImage: "face.smiling") }.tag("face")
            FeedView().tabItem { Label("Feed", systemImage: "text.bubble") }.tag("feed")
            DeckView().tabItem { Label("Ops", systemImage: "square.grid.3x2") }.tag("ops")
            SettingsView().tabItem { Label("Gateway", systemImage: "antenna.radiowaves.left.and.right") }.tag("gateway")
        }
        .tint(Phosphor.green)
    }
}

// MARK: - Face

struct FaceView: View {
    @EnvironmentObject var model: FamiliarModel
    @State private var blink = false

    private var mood: (face: String, label: String, color: Color) {
        if model.waiting > 0 { return ("( ◉ ◉ )", "APPROVAL WAITING", Phosphor.red) }
        if model.running > 0 || model.jobState == "running" {
            return ("( ◔ ◔ )", "WORKING — \(model.jobLabel.isEmpty ? "session" : model.jobLabel)", Phosphor.amber)
        }
        return ("( ◉ ◉ )", "IDLE", Phosphor.green)
    }

    var body: some View {
        ZStack {
            Phosphor.bg.ignoresSafeArea()
            VStack(spacing: 24) {
                statusStrip
                Spacer()
                Text(blink ? "( ─ ─ )" : mood.face)
                    .font(.system(size: 64, design: .monospaced))
                    .foregroundColor(mood.color)
                    .shadow(color: mood.color.opacity(0.8), radius: 12)
                Text(mood.label)
                    .font(.system(.headline, design: .monospaced))
                    .foregroundColor(mood.color)
                if !model.msg.isEmpty {
                    Text(model.msg)
                        .font(.system(.footnote, design: .monospaced))
                        .foregroundColor(Phosphor.dim)
                        .lineLimit(2)
                        .padding(.horizontal)
                }
                if let a = model.approval {
                    approvalBanner(a)
                }
                Spacer()
                vitals
            }
            .padding()
        }
        .onReceive(Timer.publish(every: 4, on: .main, in: .common).autoconnect()) { _ in
            blink = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { blink = false }
        }
    }

    private var statusStrip: some View {
        HStack {
            Circle()
                .fill(model.status == .live ? Phosphor.green :
                      model.status == .connecting ? Phosphor.amber : Phosphor.red)
                .frame(width: 10, height: 10)
            Text(model.status.rawValue.uppercased())
                .font(.system(.caption, design: .monospaced))
                .foregroundColor(Phosphor.dim)
            Spacer()
            Text("POCKET FAMILIAR")
                .font(.system(.caption, design: .monospaced))
                .foregroundColor(Phosphor.dim)
        }
    }

    private func approvalBanner(_ a: FamiliarModel.Approval) -> some View {
        VStack(spacing: 10) {
            Text("⚠ APPROVAL PENDING").bold()
            Text(a.text).lineLimit(4)
            HStack(spacing: 12) {
                Button("ALLOW ONCE") { model.resolveApproval("once") }
                    .buttonStyle(PhosphorButton(color: Phosphor.green))
                Button("DENY") { model.resolveApproval("deny") }
                    .buttonStyle(PhosphorButton(color: Phosphor.red, filled: true))
            }
        }
        .font(.system(.footnote, design: .monospaced))
        .foregroundColor(Phosphor.red)
        .padding()
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Phosphor.red, lineWidth: 1))
    }

    private var vitals: some View {
        HStack(spacing: 10) {
            vital("SESS", "\(model.total)")
            vital("RUN", "\(model.running)")
            vital("WAIT", "\(model.waiting)", warn: model.waiting > 0)
            vital("TOK", compactCount(model.tokensToday))
            vital("TOOLS", "\(model.toolsToday)")
        }
    }

    private func vital(_ label: String, _ value: String, warn: Bool = false) -> some View {
        VStack(spacing: 2) {
            Text(value)
                .font(.system(.body, design: .monospaced).bold())
                .foregroundColor(warn ? Phosphor.red : Phosphor.green)
            Text(label)
                .font(.system(size: 9, design: .monospaced))
                .foregroundColor(Phosphor.dim)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(Phosphor.dim.opacity(0.5), lineWidth: 1))
    }

    private func compactCount(_ n: Int) -> String {
        n >= 1_000_000 ? String(format: "%.1fM", Double(n) / 1_000_000)
        : n >= 1_000 ? String(format: "%.1fk", Double(n) / 1_000)
        : "\(n)"
    }
}

// MARK: - Feed

struct FeedView: View {
    @EnvironmentObject var model: FamiliarModel

    var body: some View {
        ZStack {
            Phosphor.bg.ignoresSafeArea()
            if model.feed.isEmpty {
                Text("no traffic yet")
                    .font(.system(.footnote, design: .monospaced))
                    .foregroundColor(Phosphor.dim)
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 8) {
                            ForEach(model.feed) { item in
                                HStack(alignment: .top, spacing: 8) {
                                    Text(item.role)
                                        .foregroundColor(roleColor(item.role))
                                        .frame(width: 16, alignment: .leading)
                                    Text(item.text)
                                        .foregroundColor(item.role == "!" ? Phosphor.amber : Phosphor.green)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                }
                                .font(.system(.footnote, design: .monospaced))
                                .id(item.id)
                            }
                        }
                        .padding()
                    }
                    .onChange(of: model.feed.count) { _ in
                        if let last = model.feed.last { proxy.scrollTo(last.id, anchor: .bottom) }
                    }
                }
            }
        }
    }

    private func roleColor(_ role: String) -> Color {
        switch role {
        case "u": return Phosphor.cyan
        case "!": return Phosphor.amber
        case "·": return Phosphor.dim
        default: return Phosphor.green
        }
    }
}

// MARK: - Ops (deck + job control)

struct PhosphorButton: ButtonStyle {
    var color: Color
    var filled = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(.footnote, design: .monospaced).bold())
            .foregroundColor(filled ? Phosphor.bg : color)
            .padding(.horizontal, 14).padding(.vertical, 8)
            .background(filled ? color : .clear)
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(color, lineWidth: 1))
            .opacity(configuration.isPressed ? 0.5 : 1)
    }
}

struct DeckView: View {
    @EnvironmentObject var model: FamiliarModel
    @State private var fired: Int?

    var body: some View {
        ZStack {
            Phosphor.bg.ignoresSafeArea()
            VStack(spacing: 16) {
                if model.deck.isEmpty {
                    Spacer()
                    Text("no deck yet — waiting for the gateway")
                        .font(.system(.footnote, design: .monospaced))
                        .foregroundColor(Phosphor.dim)
                } else {
                    LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 12), count: 2),
                              spacing: 12) {
                        ForEach(model.deck) { b in deckButton(b) }
                    }
                    .padding(.top, 8)
                }
                Spacer()
                if model.jobState != "idle" { jobStrip }
            }
            .padding()
        }
    }

    private func deckButton(_ b: FamiliarModel.DeckButton) -> some View {
        let color = deckColor(b.color)
        let label = VStack(spacing: 4) {
            Text(b.label)
                .font(.system(.body, design: .monospaced).bold())
            Text(b.confirm ? "▸ hold to fire" : "tap")
                .font(.system(size: 9, design: .monospaced))
                .opacity(0.6)
        }
        .foregroundColor(color)
        .frame(maxWidth: .infinity, minHeight: 72)
        .background(fired == b.id ? color.opacity(0.25) : .clear)
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(color, lineWidth: b.confirm ? 2 : 1))
        .contentShape(Rectangle())

        return Group {
            if b.confirm {
                label.onLongPressGesture(minimumDuration: 0.8) { fire(b) }
            } else {
                label.onTapGesture { fire(b) }
            }
        }
    }

    private func fire(_ b: FamiliarModel.DeckButton) {
        model.runDeck(b)
        withAnimation(.easeOut(duration: 0.4)) { fired = b.id }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { fired = nil }
    }

    private var jobStrip: some View {
        HStack {
            Text("\(model.jobState.uppercased()) \(model.jobLabel)")
                .font(.system(.footnote, design: .monospaced))
                .foregroundColor(Phosphor.amber)
                .lineLimit(1)
            Spacer()
            Button(model.jobState == "paused" ? "RESUME" : "PAUSE") { model.jobAction("pause") }
                .buttonStyle(PhosphorButton(color: Phosphor.amber))
            Button("CANCEL") { model.jobAction("cancel") }
                .buttonStyle(PhosphorButton(color: Phosphor.red))
        }
        .padding(10)
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Phosphor.amber.opacity(0.6), lineWidth: 1))
    }

    private func deckColor(_ name: String) -> Color {
        switch name {
        case "red": return Phosphor.red
        case "amber": return Phosphor.amber
        case "cyan": return Phosphor.cyan
        default: return Phosphor.green
        }
    }
}

// MARK: - Settings

struct SettingsView: View {
    @EnvironmentObject var model: FamiliarModel
    @AppStorage("host") private var host = FamiliarModel.defaultHost
    @AppStorage("port") private var port = FamiliarModel.defaultPort
    @AppStorage("token") private var token = ""

    var body: some View {
        ZStack {
            Phosphor.bg.ignoresSafeArea()
            Form {
                Section("Gateway") {
                    TextField("Host (LAN or tailnet IP)", text: $host)
                        .keyboardType(.URL)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                    TextField("Port", value: $port, format: .number.grouping(.never))
                        .keyboardType(.numberPad)
                    SecureField("Transport token", text: $token)
                }
                Section {
                    Button("Reconnect") { model.connectFromSettings() }
                        .foregroundColor(Phosphor.green)
                } footer: {
                    Text("Status: \(model.status.rawValue). Token lives in transport.token of ~/.hermes/familiar_actions.json on the gateway.")
                        .font(.system(.caption2, design: .monospaced))
                }
            }
            .scrollContentBackground(.hidden)
            .font(.system(.body, design: .monospaced))
        }
    }
}
