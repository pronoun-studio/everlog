// Role: 「いまユーザーが操作しているディスプレイ（active display）」を推定してJSONで返すCLI。
// How: 可能なら frontmost app の最前面ウィンドウ中心点→所属スクリーンを求め、だめならマウス位置でフォールバックする。
// Key entry points: このファイルのトップレベル処理（引数なし）。
// Collaboration: `everlog/display.py` が `EVERYTIME-LOG/bin/ecdisplay` を起動し、`everlog/capture.py` が JSONL に記録する。
import AppKit
import Foundation
import CoreGraphics

struct Output: Codable {
    let active_display: Int?
    let source: String
    let point: Point?
    let error: String?
}

struct Point: Codable {
    let x: Double
    let y: Double
}

func writeStderr(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

func orderedDisplayIDs() -> [CGDirectDisplayID] {
    var count: UInt32 = 0
    let rc0 = CGGetActiveDisplayList(0, nil, &count)
    if rc0 != .success || count == 0 {
        // Fallback: NSScreen list (works better in some non-interactive contexts).
        let ids = NSScreen.screens.compactMap { screen -> CGDirectDisplayID? in
            guard let n = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber else {
                return nil
            }
            return CGDirectDisplayID(truncating: n)
        }
        if ids.isEmpty {
            return []
        }
        var out = ids
        let main = CGMainDisplayID()
        if let idx = out.firstIndex(of: main) {
            out.remove(at: idx)
            out.insert(main, at: 0)
        }
        return out
    }
    var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
    let rc1 = CGGetActiveDisplayList(count, &ids, &count)
    if rc1 != .success || ids.isEmpty {
        // Same fallback as above.
        let fallback = NSScreen.screens.compactMap { screen -> CGDirectDisplayID? in
            guard let n = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber else {
                return nil
            }
            return CGDirectDisplayID(truncating: n)
        }
        if fallback.isEmpty {
            return []
        }
        var out = fallback
        let main = CGMainDisplayID()
        if let idx = out.firstIndex(of: main) {
            out.remove(at: idx)
            out.insert(main, at: 0)
        }
        return out
    }

    // `screencapture -D 1` はメインディスプレイであることが多いので、main を先頭に寄せる。
    let main = CGMainDisplayID()
    if let idx = ids.firstIndex(of: main) {
        ids.remove(at: idx)
        ids.insert(main, at: 0)
    }
    return ids
}

func displayIndex(for displayID: CGDirectDisplayID, in ordered: [CGDirectDisplayID]) -> Int? {
    guard let idx = ordered.firstIndex(of: displayID) else { return nil }
    return idx + 1 // 1-based to align with `screencapture -D`
}

func displayIDContaining(point: CGPoint) -> CGDirectDisplayID? {
    for screen in NSScreen.screens {
        let frame = screen.frame
        if frame.contains(point) {
            if let n = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber {
                return CGDirectDisplayID(truncating: n)
            }
        }
    }
    return nil
}

func frontmostWindowCenterPoint() -> CGPoint? {
    guard let app = NSWorkspace.shared.frontmostApplication else { return nil }
    let pid = Int(app.processIdentifier)

    let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let raw = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else {
        return nil
    }

    for w in raw {
        guard let ownerPID = w[kCGWindowOwnerPID as String] as? Int, ownerPID == pid else { continue }
        if let layer = w[kCGWindowLayer as String] as? Int, layer != 0 { continue }
        if let on = w[kCGWindowIsOnscreen as String] as? Int, on == 0 { continue }
        guard let b = w[kCGWindowBounds as String] as? [String: Any] else { continue }
        guard
            let x = b["X"] as? Double,
            let y = b["Y"] as? Double,
            let width = b["Width"] as? Double,
            let height = b["Height"] as? Double
        else { continue }
        // Ignore tiny/utility windows.
        if width < 80 || height < 80 { continue }
        return CGPoint(x: x + width / 2.0, y: y + height / 2.0)
    }
    return nil
}

func mousePoint() -> CGPoint? {
    // Cocoa global mouse location (origin is bottom-left of the primary display coordinate space).
    return NSEvent.mouseLocation
}

// Ensure AppKit is initialized (some APIs return empty in pure CLI contexts otherwise).
_ = NSApplication.shared

let ordered = orderedDisplayIDs()
if ordered.isEmpty {
    let out = Output(active_display: nil, source: "none", point: nil, error: "CGGetActiveDisplayList returned empty")
    let enc = JSONEncoder()
    enc.outputFormatting = [.withoutEscapingSlashes]
    if let data = try? enc.encode(out) {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write("\n".data(using: .utf8)!)
    } else {
        writeStderr("Failed to encode JSON output")
        exit(1)
    }
    exit(0)
}

var chosenPoint: CGPoint? = nil
var source = "none"

if let p = frontmostWindowCenterPoint() {
    chosenPoint = p
    source = "front_window_center"
} else if let p = mousePoint() {
    chosenPoint = p
    source = "mouse"
}

var activeDisplayIndex: Int? = nil
var pointOut: Point? = nil
var err: String? = nil

if let p = chosenPoint {
    pointOut = Point(x: Double(p.x), y: Double(p.y))
    if let displayID = displayIDContaining(point: p) {
        activeDisplayIndex = displayIndex(for: displayID, in: ordered)
        if activeDisplayIndex == nil {
            err = "Failed to map displayID to index"
        }
    } else {
        err = "No NSScreen contained the chosen point"
    }
} else {
    err = "Failed to determine a point (front window and mouse both unavailable)"
}

let out = Output(active_display: activeDisplayIndex, source: source, point: pointOut, error: err)
let enc = JSONEncoder()
enc.outputFormatting = [.withoutEscapingSlashes]
do {
    let data = try enc.encode(out)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
} catch {
    writeStderr("Failed to encode JSON: \(error)")
    exit(1)
}
