// Print the text in an image, top to bottom, using macOS's Vision framework.
// Used by tools/transcribe_media.py for carousel slides; runs locally.
//
//     swift tools/ocr.swift slide.jpg

import Foundation
import Vision

guard CommandLine.arguments.count > 1,
      let image = NSURL(fileURLWithPath: CommandLine.arguments[1]) as URL?
else {
    FileHandle.standardError.write("usage: swift ocr.swift IMAGE\n".data(using: .utf8)!)
    exit(2)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true

let handler = VNImageRequestHandler(url: image, options: [:])
do {
    try handler.perform([request])
} catch {
    FileHandle.standardError.write("\(error)\n".data(using: .utf8)!)
    exit(1)
}

// Vision's origin is bottom-left; sort by descending y for reading order.
let lines = (request.results ?? [])
    .sorted { $0.boundingBox.origin.y > $1.boundingBox.origin.y }
    .compactMap { $0.topCandidates(1).first?.string }
print(lines.joined(separator: "\n"))
