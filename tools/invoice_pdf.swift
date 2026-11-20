// Render one creator invoice as a single-page PDF, in the layout creators
// already send Readwise (invoice number and dates; From / To; description and
// amount; total; Pay To). Reads a JSON spec on stdin, writes the PDF to the
// path given. Uses only macOS's own text drawing. The spec can hold bank
// numbers: this program never prints it. Built and driven by
// tools/payments_due.py.
//
//     invoice_pdf out.pdf < spec.json

import AppKit
import Foundation

struct Line: Decodable { let description: String; let amount: String }
struct Spec: Decodable {
    let number: String
    let issued: String
    let due: String
    let from: [String]
    let to: [String]
    let lines: [Line]
    let total: String
    let payTo: [String]
}

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write("usage: invoice_pdf OUT.pdf < spec.json\n".data(using: .utf8)!)
    exit(2)
}
let spec: Spec
do {
    spec = try JSONDecoder().decode(Spec.self, from: FileHandle.standardInput.readDataToEndOfFile())
} catch {
    FileHandle.standardError.write("bad spec: \(error)\n".data(using: .utf8)!)
    exit(1)
}

var page = CGRect(x: 0, y: 0, width: 612, height: 792)       // US Letter
let url = URL(fileURLWithPath: CommandLine.arguments[1]) as CFURL
guard let pdf = CGContext(url, mediaBox: &page, nil) else { exit(1) }
pdf.beginPDFPage(nil)
NSGraphicsContext.current = NSGraphicsContext(cgContext: pdf, flipped: false)

let ink = NSColor(calibratedWhite: 0.12, alpha: 1)
let soft = NSColor(calibratedWhite: 0.45, alpha: 1)
func font(_ size: CGFloat, _ weight: NSFont.Weight = .regular) -> NSFont {
    NSFont.systemFont(ofSize: size, weight: weight)
}
let margin: CGFloat = 60
var y: CGFloat = 792 - 70                                    // drawing downwards

@discardableResult
func text(_ s: String, x: CGFloat, _ f: NSFont, _ c: NSColor = ink, width: CGFloat = 240,
          align: NSTextAlignment = .left) -> CGFloat {
    let style = NSMutableParagraphStyle()
    style.alignment = align
    let attrs: [NSAttributedString.Key: Any] = [.font: f, .foregroundColor: c, .paragraphStyle: style]
    let box = (s as NSString).boundingRect(with: NSSize(width: width, height: 1000),
                                           options: [.usesLineFragmentOrigin], attributes: attrs)
    (s as NSString).draw(with: NSRect(x: x, y: y - box.height, width: width, height: box.height),
                         options: [.usesLineFragmentOrigin], attributes: attrs)
    return box.height
}
func rule(_ at: CGFloat, weight: CGFloat = 0.6) {
    pdf.setStrokeColor(NSColor(calibratedWhite: 0.8, alpha: 1).cgColor)
    pdf.setLineWidth(weight)
    pdf.move(to: CGPoint(x: margin, y: at))
    pdf.addLine(to: CGPoint(x: 612 - margin, y: at))
    pdf.strokePath()
}

// Title and the number / dates block.
text("INVOICE", x: margin, font(24, .semibold))
let col2: CGFloat = 612 - margin - 200
for (label, value) in [("Invoice #", spec.number), ("Issue Date", spec.issued), ("Due Date", spec.due)] {
    text(label, x: col2, font(10), soft, width: 90)
    text(value, x: col2 + 90, font(10, .medium), width: 110, align: .right)
    y -= 16
}
y -= 26

// From / To.
text("From:", x: margin, font(10, .semibold), soft)
text("To:", x: 330, font(10, .semibold), soft)
y -= 16
let top = y
var lowest = y
for line in spec.from { y -= text(line, x: margin, font(11)) + 2 }
lowest = min(lowest, y)
y = top
for line in spec.to { y -= text(line, x: 330, font(11)) + 2 }
y = min(lowest, y) - 34

// Description / amount.
text("Description:", x: margin, font(10, .semibold), soft)
text("Amount:", x: 612 - margin - 120, font(10, .semibold), soft, width: 120, align: .right)
y -= 18
rule(y + 4)
y -= 8
for line in spec.lines {
    let h = text(line.description, x: margin, font(11), width: 360)
    text(line.amount, x: 612 - margin - 120, font(11), width: 120, align: .right)
    y -= h + 10
}
rule(y + 4)
y -= 10
text("Total owed:", x: 612 - margin - 260, font(12, .semibold), width: 140, align: .right)
text(spec.total, x: 612 - margin - 120, font(12, .semibold), width: 120, align: .right)
y -= 50

// Pay to.
text("Pay To:", x: margin, font(10, .semibold), soft)
y -= 16
for line in spec.payTo { y -= text(line, x: margin, font(11), width: 400) + 2 }

pdf.endPDFPage()
pdf.closePDF()
