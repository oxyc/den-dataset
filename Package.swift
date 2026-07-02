// swift-tools-version:5.9
import PackageDescription

// den-dataset — the standalone "dataset producer" extracted from the Den tvOS app. It builds the shipped
// discovery index (labels JSON + int8 vector blob) with the SAME calibrated classifier + FNV embedder +
// quantizer as the app, but has NO dependency on DenKit: it carries its own copies of the small shared
// types and a thin TMDB client. The only coupling to the app is the artifact FORMAT.
let package = Package(
    name: "den-dataset",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "DenDataset", targets: ["DenDataset"]),
        .executable(name: "taxonomy-backfill", targets: ["taxonomy-backfill"]),
    ],
    targets: [
        .target(name: "DenDataset"),
        .executableTarget(
            name: "taxonomy-backfill",
            dependencies: ["DenDataset"]
        ),
        .testTarget(
            name: "DenDatasetTests",
            dependencies: ["DenDataset"]
        ),
    ]
)
