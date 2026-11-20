// Record the Claude desktop app's window, with only Claude's own audio, for N
// seconds — how an Instagram reel is captured: played in the app's browser
// pane and recorded as shown, the way Erin would screen-record her phone.
// Other apps' windows and sound never reach the file, even if they are on
// top. Needs Screen Recording permission for Claude (System Settings →
// Privacy & Security). Built and driven by tools/capture.py.
//
//   screen_record <out.mp4> <seconds>
// Prints "recording <unix time>" when frames start, "finished" at the end.
import AppKit
import AVFoundation
import ScreenCaptureKit

let args = CommandLine.arguments
guard args.count == 3, let seconds = Double(args[2]) else {
    print("usage: screen_record <out.mp4> <seconds>"); exit(2)
}
let out = URL(fileURLWithPath: args[1])

final class Rec: NSObject, SCRecordingOutputDelegate, SCStreamDelegate {
    func recordingOutputDidStartRecording(_ r: SCRecordingOutput) {
        print(String(format: "recording %.3f", Date().timeIntervalSince1970)); fflush(stdout)
    }
    func recordingOutputDidFinishRecording(_ r: SCRecordingOutput) {
        print("finished"); fflush(stdout); exit(0)
    }
    func recordingOutput(_ r: SCRecordingOutput, didFailWithError e: Error) {
        print("failed: \(e)"); exit(1)
    }
    func stream(_ s: SCStream, didStopWithError e: Error) {
        print("stream stopped: \(e)"); exit(1)
    }
}
let rec = Rec()

Task {
    do {
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: true)
        let claude = content.windows
            .filter { $0.owningApplication?.bundleIdentifier.hasPrefix("com.anthropic.claude") == true }
            .max { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height }
        guard let win = claude else { print("no Claude window on screen"); exit(1) }
        let center = CGPoint(x: win.frame.midX, y: win.frame.midY)
        guard let display = content.displays.first(where: { $0.frame.contains(center) })
                ?? content.displays.first else { print("no display"); exit(1) }

        // Only Claude's own windows and Claude's own audio: other apps on top
        // of it, and their sound, never reach the recording.
        guard let app = win.owningApplication else { print("no owning app"); exit(1) }
        let filter = SCContentFilter(display: display, including: [app], exceptingWindows: [])
        let scale = CGFloat(filter.pointPixelScale)
        let f = win.frame
        print(String(format: "window %.0f %.0f %.0f %.0f display %.0f %.0f scale %.1f",
                     f.minX - display.frame.minX, f.minY - display.frame.minY,
                     f.width, f.height, display.frame.minX, display.frame.minY, scale))

        let cfg = SCStreamConfiguration()
        let src = CGRect(x: f.minX - display.frame.minX, y: f.minY - display.frame.minY,
                         width: f.width, height: f.height)
        cfg.sourceRect = src
        cfg.width = Int(src.width * scale)
        cfg.height = Int(src.height * scale)
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 30)
        cfg.showsCursor = false
        cfg.capturesAudio = true
        cfg.sampleRate = 48000
        cfg.channelCount = 2

        let stream = SCStream(filter: filter, configuration: cfg, delegate: rec)
        let rc = SCRecordingOutputConfiguration()
        rc.outputURL = out
        rc.outputFileType = .mp4
        rc.videoCodecType = .h264
        try stream.addRecordingOutput(SCRecordingOutput(configuration: rc, delegate: rec))
        try await stream.startCapture()
        try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
        try await stream.stopCapture()
        try await Task.sleep(nanoseconds: 5_000_000_000)
        print("no finish callback"); exit(1)
    } catch {
        print("error: \(error)"); exit(1)
    }
}
RunLoop.main.run()
