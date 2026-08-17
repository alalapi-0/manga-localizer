import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!

    func applicationDidFinishLaunching(_ notification: Notification) {
        let raw = CommandLine.arguments.dropFirst().first ?? "http://127.0.0.1:8000"
        guard let url = URL(string: raw) else {
            NSApp.terminate(nil)
            return
        }
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1280, height: 800)
        let width = min(1440, screen.width * 0.9)
        let height = min(900, screen.height * 0.9)
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: width, height: height),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Manga Localizer"
        window.center()
        let webView = WKWebView(frame: window.contentView?.bounds ?? .zero)
        webView.autoresizingMask = [.width, .height]
        window.contentView = webView
        webView.load(URLRequest(url: url))
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
