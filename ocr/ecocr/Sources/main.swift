// Role: 画像をVision FrameworkでOCRし、`{\"text\":\"...\"}` をstdoutに出力するCLI。
// How: NSImageからCGImageを取り、VNRecognizeTextRequestで認識した行を結合してJSONとして出力する。
// Key entry points: このファイルのトップレベル処理（引数=画像パス）。
// Collaboration: `everlog/capture.py` がスクショを作り、`everlog/ocr.py` 経由でこのバイナリを起動して結果を受け取る。
import AppKit
import Foundation
import Vision

struct Output: Codable {
    let text: String
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

guard CommandLine.arguments.count >= 2 else {
    fail("Usage: ecocr /path/to/image.png")
}

let path = CommandLine.arguments[1]
let url = URL(fileURLWithPath: path)
guard let image = NSImage(contentsOf: url) else {
    fail("Failed to load image: \(path)")
}

guard
    let tiff = image.tiffRepresentation,
    let bitmap = NSBitmapImageRep(data: tiff),
    let cgImage = bitmap.cgImage
else {
    fail("Failed to get CGImage for: \(path)")
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["ja-JP", "en-US"]

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
} catch {
    fail("Vision OCR failed: \(error)")
}

let observations = request.results ?? []
let lines = observations.compactMap { $0.topCandidates(1).first?.string }
let text = lines.joined(separator: "\n")

let out = Output(text: text)
let enc = JSONEncoder()
enc.outputFormatting = [.withoutEscapingSlashes]
do {
    let data = try enc.encode(out)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
} catch {
    fail("Failed to encode JSON: \(error)")
}
