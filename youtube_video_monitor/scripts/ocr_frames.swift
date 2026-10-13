import AppKit
import Foundation
import Vision

struct OCRRecord: Codable {
    let path: String
    let lines: [String]
}

var records: [OCRRecord] = []
for argument in CommandLine.arguments.dropFirst() {
    let url = URL(fileURLWithPath: argument)
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    do {
        try VNImageRequestHandler(url: url).perform([request])
        let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
        records.append(OCRRecord(path: argument, lines: lines))
    } catch {
        records.append(OCRRecord(path: argument, lines: []))
    }
}
let data = try JSONEncoder().encode(records)
print(String(data: data, encoding: .utf8) ?? "[]")
