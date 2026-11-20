// Print the text of a PDF, page by page, using macOS's own PDFKit. A page
// with no text layer (a scanned or photographed invoice) is rendered and read
// with Vision instead. Runs locally; nothing leaves the machine. Built and
// driven by tools/import_invoices.py.
//
//     pdf_text invoice.pdf

import AppKit
import Foundation
import PDFKit
import Vision

guard CommandLine.arguments.count > 1 else {
    FileHandle.standardError.write("usage: pdf_text FILE.pdf\n".data(using: .utf8)!)
    exit(2)
}
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let document = PDFDocument(url: url) else {
    FileHandle.standardError.write("not a readable PDF\n".data(using: .utf8)!)
    exit(1)
}

func recognise(_ page: PDFPage) -> String {
    let box = page.bounds(for: .mediaBox)
    let scale: CGFloat = 3    // ~216 dpi: small print on invoices needs it
    let size = NSSize(width: box.width * scale, height: box.height * scale)
    let image = page.thumbnail(of: size, for: .mediaBox)
    guard let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return "" }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false   // numbers must come through as written
    do {
        try VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
    } catch {
        return ""
    }
    return (request.results ?? [])
        .sorted { $0.boundingBox.origin.y > $1.boundingBox.origin.y }
        .compactMap { $0.topCandidates(1).first?.string }
        .joined(separator: "\n")
}

var pages: [String] = []
for i in 0..<document.pageCount {
    guard let page = document.page(at: i) else { continue }
    let text = page.string?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    pages.append(text.isEmpty ? recognise(page) : text)
}
print(pages.joined(separator: "\n\n"))
