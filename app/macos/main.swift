// Surya Memory for macOS: a window around the local memory app.
//
// Starts the bundled Python server, waits for it to print its address, and shows
// that page. All the logic lives in the server. This file only makes it an app.

import Cocoa
import WebKit

let resources = Bundle.main.resourcePath ?? ""
let selfTest = ProcessInfo.processInfo.environment["SURYA_MEMORY_SELFTEST"] == "1"

func trace(_ line: String) {
    if selfTest { fputs("selftest: \(line)\n", stderr) }
}

func rgb(_ hex: UInt32) -> NSColor {
    NSColor(srgbRed: CGFloat(hex >> 16 & 255) / 255, green: CGFloat(hex >> 8 & 255) / 255, blue: CGFloat(hex & 255) / 255, alpha: 1)
}

// The sun on the line being written, the same drawing as the page's mark.
func drawIcon(size: CGFloat) -> NSImage {
    NSImage(size: NSSize(width: size, height: size), flipped: true) { rect in
        let plate = rect.insetBy(dx: size * 0.098, dy: size * 0.098)
        let squircle = NSBezierPath(roundedRect: plate, xRadius: size * 0.18, yRadius: size * 0.18)
        NSGradient(starting: rgb(0x221D17), ending: rgb(0x0E0C0A))?.draw(in: squircle, angle: 90)

        // The page's mark is drawn on a 32 unit square. Here it fills the plate's middle.
        let unit = plate.width * 0.72 / 32
        let origin = NSPoint(x: plate.midX - 16 * unit, y: plate.midY - 16 * unit)
        let sun = NSPoint(x: origin.x + 21.5 * unit, y: origin.y + 16 * unit)
        NSGradient(colors: [rgb(0xFFD977).withAlphaComponent(0.42), rgb(0xFFD977).withAlphaComponent(0)])?
            .draw(fromCenter: sun, radius: 0, toCenter: sun, radius: 12.5 * unit, options: [])
        let bars = NSGradient(starting: rgb(0xF2B04A), ending: rgb(0xFFD977))
        for (y, width) in [(6.4, 24.0), (14.3, 11.0), (22.2, 17.0)] as [(CGFloat, CGFloat)] {
            let bar = NSRect(x: origin.x + 4 * unit, y: origin.y + y * unit, width: width * unit, height: 3.4 * unit)
            bars?.draw(in: NSBezierPath(roundedRect: bar, xRadius: 1.7 * unit, yRadius: 1.7 * unit), angle: 0)
        }
        let radius = 5 * unit
        let disc = NSBezierPath(ovalIn: NSRect(x: sun.x - radius, y: sun.y - radius, width: radius * 2, height: radius * 2))
        NSGradient(colors: [rgb(0xFFE8A0), rgb(0xF2B04A), rgb(0xD9891F)], atLocations: [0, 0.6, 1], colorSpace: .sRGB)?
            .draw(in: disc, relativeCenterPosition: NSPoint(x: -0.28, y: -0.4))
        return true
    }
}

// The strip across the top of the window that the traffic lights sit in. It takes
// mouse downs so the window can be dragged, and lets everything else reach the page.
final class DragStrip: NSView {
    override func hitTest(_ point: NSPoint) -> NSView? {
        NSApp.currentEvent?.type == .leftMouseDown ? super.hitTest(point) : nil
    }
    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 { window?.performZoom(nil) } else { window?.performDrag(with: event) }
    }
}

final class App: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var web: WKWebView!
    let server = Process()
    let parentAlive = Pipe()
    var origin = ""

    func applicationDidFinishLaunching(_ note: Notification) {
        trace("launched")
        NSApp.applicationIconImage = drawIcon(size: 512)
        buildMenu()
        buildWindow()
        startServer()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { !selfTest }

    func applicationWillTerminate(_ note: Notification) {
        trace("terminating")
        if server.isRunning { server.terminate() }
    }

    // A WKWebView has no copy or paste unless the app's menu provides them.
    func buildMenu() {
        let bar = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Hide Surya Memory", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Surya Memory", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        bar.addItem(appItem)
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        bar.addItem(editItem)
        let windowItem = NSMenuItem()
        let windows = NSMenu(title: "Window")
        windows.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        windows.addItem(withTitle: "Minimise", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        windowItem.submenu = windows
        bar.addItem(windowItem)
        NSApp.mainMenu = bar
    }

    func buildWindow() {
        let config = WKWebViewConfiguration()
        config.userContentController.addUserScript(WKUserScript(source: "document.documentElement.dataset.shell = 'mac'", injectionTime: .atDocumentStart, forMainFrameOnly: true))
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.isHidden = true  // until the page has painted, so the window never flashes white
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 780),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
        window.title = "Surya Memory"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.backgroundColor = NSColor(name: nil) { $0.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? rgb(0x0B0A09) : rgb(0xF8F7F3) }
        window.minSize = NSSize(width: 860, height: 560)  // the page's side rail needs this much
        let root = NSView(frame: NSRect(x: 0, y: 0, width: 1180, height: 780))
        web.frame = root.bounds
        web.autoresizingMask = [.width, .height]
        root.addSubview(web)
        let strip = DragStrip(frame: NSRect(x: 0, y: root.bounds.height - 38, width: root.bounds.width, height: 38))
        strip.autoresizingMask = [.width, .minYMargin]
        root.addSubview(strip)
        window.contentView = root
        window.center()
        window.setFrameAutosaveName("main")
        if !selfTest {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    func startServer() {
        let output = Pipe()
        server.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        var args = [resources + "/scripts/surya-memory", "app", "--no-open", "--exit-with-parent"]
        if let dir = ProcessInfo.processInfo.environment["SURYA_MEMORY_APP_DIR"] { args += ["--dir", dir] }
        server.arguments = args
        server.standardOutput = output
        server.standardInput = parentAlive  // closes if this app dies, and the server exits with it
        server.terminationHandler = { [weak self] process in
            guard process.terminationStatus != 0 else { return }
            DispatchQueue.main.async { self?.report("The memory server stopped. It needs git and Python 3.9 or newer. To install them, run: xcode-select --install") }
        }
        var buffer = Data()
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            buffer.append(handle.availableData)
            guard let text = String(data: buffer, encoding: .utf8), let range = text.range(of: "SURYA_MEMORY_URL=") else { return }
            let rest = text[range.upperBound...]
            guard let end = rest.firstIndex(of: "\n"), let url = URL(string: String(rest[..<end])) else { return }
            handle.readabilityHandler = nil
            trace("server ready")
            DispatchQueue.main.async {
                self?.origin = "\(url.scheme ?? "http")://\(url.host ?? "127.0.0.1"):\(url.port ?? 0)"
                self?.web.load(URLRequest(url: url))
            }
        }
        do { try server.run() } catch { report("Couldn't start the memory server: \(error.localizedDescription)") }
    }

    func report(_ message: String) {
        if selfTest { print("SELFTEST FAIL: \(message)"); exit(1) }
        let alert = NSAlert()
        alert.messageText = "Surya Memory can't start"
        alert.informativeText = message
        alert.runModal()
        NSApp.terminate(nil)
    }

    // Only the local page loads in this window. Links in a memory open in the browser.
    func webView(_ view: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if url.absoluteString.hasPrefix(origin + "/") || url.scheme == "about" { return decisionHandler(.allow) }
        if url.scheme == "http" || url.scheme == "https" { NSWorkspace.shared.open(url) }
        decisionHandler(.cancel)
    }

    func webView(_ view: WKWebView, createWebViewWith config: WKWebViewConfiguration, for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, url.scheme == "http" || url.scheme == "https" { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ view: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ view: WKWebView, didFinish navigation: WKNavigation!) {
        view.isHidden = false
        guard selfTest else { return }
        trace("page loaded")
        // Give the page a moment to fetch its state, then report what it shows.
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
            let probe = "[document.title, document.documentElement.dataset.shell, document.querySelectorAll('nav button, nav a').length, document.body.innerText.slice(0, 200), getComputedStyle(document.body).backgroundColor].join(' | ')"
            view.evaluateJavaScript(probe) { value, error in
                print(error == nil ? "SELFTEST OK: \(value ?? "")" : "SELFTEST FAIL: \(error!)")
                let finish = {
                    if self.server.isRunning { self.server.terminate() }
                    exit(error == nil ? 0 : 1)
                }
                guard let out = ProcessInfo.processInfo.environment["SURYA_MEMORY_SNAPSHOT"] else { return finish() }
                view.takeSnapshot(with: nil) { image, _ in
                    if let image, let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff) {
                        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: out))
                    }
                    finish()
                }
            }
        }
    }
}

// build.sh calls this to make the Finder icon from the same drawing.
if CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--make-icon" {
    let folder = CommandLine.arguments[2]
    try? FileManager.default.createDirectory(atPath: folder, withIntermediateDirectories: true)
    for (name, size) in [("16x16", 16), ("16x16@2x", 32), ("32x32", 32), ("32x32@2x", 64), ("128x128", 128), ("128x128@2x", 256), ("256x256", 256), ("256x256@2x", 512), ("512x512", 512), ("512x512@2x", 1024)] {
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        drawIcon(size: CGFloat(size)).draw(in: NSRect(x: 0, y: 0, width: size, height: size))
        NSGraphicsContext.restoreGraphicsState()
        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: "\(folder)/icon_\(name).png"))
    }
    exit(0)
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(selfTest ? .accessory : .regular)
app.run()
