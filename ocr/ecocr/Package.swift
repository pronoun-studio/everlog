// swift-tools-version: 5.9
// Role: Vision OCRヘルパー `ecocr` のSwiftPM定義。
// How: macOS向け実行バイナリとしてビルドできるようにし、Python側から子プロセスとして起動できる形にする。
// Key entry points: なし（ビルド設定のみ）。
// Collaboration: ビルド成果物 `ecocr` を `EVERYTIME-LOG/bin/ecocr` に配置し、`everlog/ocr.py` が実行する。
import PackageDescription

let package = Package(
    name: "ecocr",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(name: "ecocr", targets: ["ecocr"])
    ],
    targets: [
        .executableTarget(
            name: "ecocr",
            path: "Sources"
        )
    ]
)
