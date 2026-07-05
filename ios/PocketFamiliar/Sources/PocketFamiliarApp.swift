import SwiftUI

@main
struct PocketFamiliarApp: App {
    @StateObject private var model = FamiliarModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                .preferredColorScheme(.dark)
                .onAppear { model.connectFromSettings() }
        }
    }
}
