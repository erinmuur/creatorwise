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

/// The page drawn onto opaque white at ~216 dpi (small print on invoices
/// needs it). Drawing it ourselves, rather than taking PDFKit's thumbnail,
/// avoids transparent backgrounds.
func render(_ page: PDFPage, scale: CGFloat = 3) -> CGImage? {
    let box = page.bounds(for: .mediaBox)
    let w = Int(box.width * scale), h = Int(box.height * scale)
    guard w > 0, h > 0, let ctx = CGContext(
        data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return nil }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: ctx)
    return ctx.makeImage()
}

/// Vision's text recognition, given `seconds` to finish. Its accurate mode
/// was seen to hang — no CPU, never returning — on a nearly blank invoice
/// page (2026-11-19), so it runs on its own thread and is abandoned on time.
func vision(_ image: CGImage, _ level: VNRequestTextRecognitionLevel, seconds: Double) -> String? {
    var lines: [String]? = nil
    let done = DispatchSemaphore(value: 0)
    Thread {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = level
        request.usesLanguageCorrection = false   // numbers must come through as written
        if (try? VNImageRequestHandler(cgImage: image, options: [:]).perform([request])) != nil {
            lines = (request.results ?? [])
                .sorted { $0.boundingBox.origin.y > $1.boundingBox.origin.y }
                .compactMap { $0.topCandidates(1).first?.string }
        }
        done.signal()
    }.start()
    if done.wait(timeout: .now() + seconds) == .timedOut { return nil }
    return lines?.joined(separator: "\n") ?? ""
}

func recognise(_ page: PDFPage, number: Int) -> String {
    guard let image = render(page) else { return "" }
    if let text = vision(image, .accurate, seconds: 25) { return text }
    FileHandle.standardError.write("page \(number): accurate text recognition timed out; using fast\n"
        .data(using: .utf8)!)
    return vision(image, .fast, seconds: 25) ?? ""
}

var pages: [String] = []
for i in 0..<document.pageCount {
    guard let page = document.page(at: i) else { continue }
    let text = page.string?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    pages.append(text.isEmpty ? recognise(page, number: i + 1) : text)
}
print(pages.joined(separator: "\n\n"))
exit(0)   // don't wait on a recognition thread that never returned
