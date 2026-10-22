// apple/AURAiPhone/Services/AuraApiService.swift
import Foundation

public struct AuditRecordItem: Codable, Identifiable, Sendable {
    public let id: String
    public let actionId: String
    public let userId: String
    public let actionType: String?
    public let category: String?
    public let timestamp: Double
    public let status: String?

    private enum CodingKeys: String, CodingKey {
        case actionId = "action_id"
        case userId = "user_id"
        case actionType = "action_type"
        case category
        case timestamp
        case status
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let aId = (try? container.decode(String.self, forKey: .actionId)) ?? UUID().uuidString
        self.id = aId
        self.actionId = aId
        self.userId = (try? container.decode(String.self, forKey: .userId)) ?? ""
        self.actionType = try? container.decode(String.self, forKey: .actionType)
        self.category = try? container.decode(String.self, forKey: .category)
        self.timestamp = (try? container.decode(Double.self, forKey: .timestamp)) ?? Date().timeIntervalSince1970
        self.status = try? container.decode(String.self, forKey: .status)
    }
}

public final class AuraApiService: ObservableObject {
    public static let shared = AuraApiService()
    private let baseURL: URL

    public init(baseURL: URL = URL(string: "https://trujdwjp17.execute-api.us-east-1.amazonaws.com")!) {
        self.baseURL = baseURL
    }

    private func makeRequest(path: String, method: String, body: [String: Any]? = nil) -> URLRequest {
        let url = baseURL.appendingPathComponent(path)
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        if let token = AuthService.shared.getAccessToken() {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }

        if let body = body {
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        return request
    }

    public func registerDevice(deviceType: String, pushToken: String, deviceId: String) async throws -> [String: Any] {
        let userId = AuthService.shared.currentUserId ?? "usr_default"
        let request = makeRequest(
            path: "api/v1/devices/wearable",
            method: "POST",
            body: [
                "user_id": userId,
                "device_id": deviceId,
                "device_type": deviceType,
                "push_token": pushToken,
                "platform": "apple_watch"
            ]
        )
        let (data, _) = try await URLSession.shared.data(for: request)
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    public func startFocusSession(durationMinutes: Int) async throws -> [String: Any] {
        let userId = AuthService.shared.currentUserId ?? "usr_default"
        let request = makeRequest(
            path: "api/v1/focus/session",
            method: "POST",
            body: [
                "user_id": userId,
                "action": "start",
                "duration_minutes": durationMinutes
            ]
        )
        let (data, _) = try await URLSession.shared.data(for: request)
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    public func reportDistracted(sessionId: String?) async throws -> [String: Any] {
        let userId = AuthService.shared.currentUserId ?? "usr_default"
        let request = makeRequest(
            path: "api/v1/focus/signal",
            method: "POST",
            body: [
                "user_id": userId,
                "session_id": sessionId ?? "",
                "explicit_signal": "self_report_distracted"
            ]
        )
        let (data, _) = try await URLSession.shared.data(for: request)
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    public func undoLastAction(undoToken: String) async throws -> [String: Any] {
        let userId = AuthService.shared.currentUserId ?? "usr_default"
        let request = makeRequest(
            path: "api/v1/actions/undo",
            method: "POST",
            body: [
                "user_id": userId,
                "undo_token": undoToken
            ]
        )
        let (data, _) = try await URLSession.shared.data(for: request)
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    public func sendNotificationFeedback(cardId: String, calendarEventId: String, action: String, seriesId: String? = nil) async throws -> [String: Any] {
        let userId = AuthService.shared.currentUserId ?? "usr_default"
        var body: [String: Any] = [
            "user_id": userId,
            "card_id": cardId,
            "calendar_event_id": calendarEventId,
            "action": action
        ]
        if let seriesId = seriesId {
            body["series_id"] = seriesId
        }

        let request = makeRequest(path: "api/v1/notifications/feedback", method: "POST", body: body)
        let (data, _) = try await URLSession.shared.data(for: request)
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    public func getAuditRecords(limit: Int = 20) async throws -> [AuditRecordItem] {
        let userId = AuthService.shared.currentUserId ?? "usr_default"
        let request = makeRequest(path: "api/v1/audit/actions?user_id=\(userId)&limit=\(limit)", method: "GET")
        let (data, _) = try await URLSession.shared.data(for: request)
        guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let records = json["audit_records"] as? [[String: Any]] else {
            return []
        }
        let recordsData = try JSONSerialization.data(withJSONObject: records)
        return (try? JSONDecoder().decode([AuditRecordItem].self, from: recordsData)) ?? []
    }

    public func getPreferences() async throws -> [String: Any] {
        let userId = AuthService.shared.currentUserId ?? "usr_default"
        let request = makeRequest(path: "api/v1/preferences?user_id=\(userId)", method: "GET")
        let (data, _) = try await URLSession.shared.data(for: request)
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    public func updatePreferences(patch: [String: Any]) async throws -> [String: Any] {
        let userId = AuthService.shared.currentUserId ?? "usr_default"
        let request = makeRequest(
            path: "api/v1/preferences",
            method: "PATCH",
            body: [
                "user_id": userId,
                "preferences": patch
            ]
        )
        let (data, _) = try await URLSession.shared.data(for: request)
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }
}
