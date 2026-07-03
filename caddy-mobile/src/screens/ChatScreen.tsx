import { useEffect, useRef, useState } from "react";
import {
  ActivityIndicator, Alert, FlatList, KeyboardAvoidingView, Platform,
  StyleSheet, Text, TextInput, TouchableOpacity, View,
} from "react-native";
import * as Location from "expo-location";
import * as ImagePicker from "expo-image-picker";
import {
  api, ChatEvent, ChatResponse, Location as Loc, RoundState, User, WeatherSnapshot,
} from "../api";
import { clearToken } from "../auth";
import { colors } from "../theme";

type Message = { role: "user" | "assistant"; content: string };

export default function ChatScreen({ user, onLogout }: { user: User; onLogout: () => void }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [roundState, setRoundState] = useState<RoundState>({ hole_scores: [], current_hole: 1 });
  const [weather, setWeather] = useState<WeatherSnapshot | null>(null);
  const [gpsYards, setGpsYards] = useState<number | null>(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const listRef = useRef<FlatList<Message>>(null);
  const hasLocationPermission = useRef(false);

  useEffect(() => {
    (async () => {
      // History + round state
      try {
        const h = await api.caddy.history();
        setMessages(h.history);
        setRoundState(h.round_state);
      } catch {}
      // Location permission up front — every message wants a fix (auto-wind,
      // auto-yardage, GPS shot tracking all depend on it)
      const { status } = await Location.requestForegroundPermissionsAsync();
      hasLocationPermission.current = status === "granted";
      const loc = await currentLocation();
      if (loc) {
        try {
          const w = await api.caddy.weather(loc.lat, loc.lng);
          setWeather(w.weather);
        } catch {}
      }
    })();
  }, []);

  async function currentLocation(): Promise<Loc> {
    if (!hasLocationPermission.current) return null;
    try {
      const pos = await Location.getCurrentPositionAsync({
        accuracy: Location.Accuracy.Balanced,
      });
      return { lat: pos.coords.latitude, lng: pos.coords.longitude };
    } catch {
      const last = await Location.getLastKnownPositionAsync();
      return last ? { lat: last.coords.latitude, lng: last.coords.longitude } : null;
    }
  }

  function applyResponse(r: ChatResponse) {
    setMessages((m) => [...m, { role: "assistant", content: r.reply }]);
    if (r.round_state) setRoundState(r.round_state);
    if (r.weather) setWeather(r.weather);
    setGpsYards(extractGpsYards(r.events));
  }

  async function handleSend() {
    const text = input.trim();
    if (!text || sending) return;
    setInput("");
    setError(null);
    setSending(true);
    setMessages((m) => [...m, { role: "user", content: text }]);
    try {
      applyResponse(await api.caddy.message(text, await currentLocation()));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Send failed");
    } finally {
      setSending(false);
    }
  }

  async function handlePhoto() {
    if (sending) return;
    const perm = await ImagePicker.requestCameraPermissionsAsync();
    const picker = perm.granted
      ? ImagePicker.launchCameraAsync
      : ImagePicker.launchImageLibraryAsync; // simulator / permission-denied fallback
    const result = await picker({ quality: 0.6 });
    if (result.canceled || !result.assets?.[0]) return;
    const note = input.trim();
    setInput("");
    setError(null);
    setSending(true);
    setMessages((m) => [...m, { role: "user", content: note || "📷 Photo" }]);
    try {
      applyResponse(await api.caddy.photo(result.assets[0].uri, note || undefined, await currentLocation()));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Photo failed");
    } finally {
      setSending(false);
    }
  }

  function handleReset() {
    Alert.alert("New conversation?", "The current chat is archived, round state cleared.", [
      { text: "Cancel", style: "cancel" },
      {
        text: "Reset",
        style: "destructive",
        onPress: async () => {
          try {
            await api.caddy.reset();
            setMessages([]);
            setRoundState({ hole_scores: [], current_hole: 1 });
            setGpsYards(null);
          } catch (err) {
            setError(err instanceof Error ? err.message : "Reset failed");
          }
        },
      },
    ]);
  }

  async function handleLogout() {
    await clearToken();
    onLogout();
  }

  const courseName = roundState.course?.club_name;
  const cur = weather?.current;

  return (
    <View style={styles.container}>
      {/* Header */}
      <View style={styles.header}>
        <Text style={styles.headerTitle}>CADDY</Text>
        <View style={styles.headerButtons}>
          <TouchableOpacity onPress={handleReset}>
            <Text style={styles.headerAction}>New</Text>
          </TouchableOpacity>
          <TouchableOpacity onPress={handleLogout}>
            <Text style={styles.headerAction}>Sign out</Text>
          </TouchableOpacity>
        </View>
      </View>

      {/* Round bar */}
      {courseName && (
        <View style={styles.roundBar}>
          <Text style={styles.roundBarText} numberOfLines={1}>
            {courseName}
            {roundState.tee?.tee_name ? ` · ${roundState.tee.tee_name}` : ""}
            {` · ${formatHoleStatus(roundState)}`}
            {gpsYards != null ? ` · ~${gpsYards} yds to green` : ""}
          </Text>
        </View>
      )}

      {/* Weather strip */}
      {cur && (
        <View style={styles.weatherStrip}>
          <Text style={styles.weatherText} numberOfLines={1}>
            {cur.temperature != null ? `${cur.temperature}°${cur.temperature_unit || "F"}` : ""}
            {cur.wind_speed ? `  ·  wind ${cur.wind_speed} ${cur.wind_direction || ""}` : ""}
            {cur.short_forecast ? `  ·  ${cur.short_forecast}` : ""}
          </Text>
        </View>
      )}

      {/* Messages */}
      <FlatList
        ref={listRef}
        style={styles.list}
        contentContainerStyle={styles.listContent}
        data={messages}
        keyExtractor={(_, i) => String(i)}
        onContentSizeChange={() => listRef.current?.scrollToEnd({ animated: true })}
        renderItem={({ item }) => (
          <View
            style={[
              styles.bubble,
              item.role === "user" ? styles.bubbleUser : styles.bubbleAssistant,
            ]}
          >
            <Text style={item.role === "user" ? styles.bubbleUserText : styles.bubbleAssistantText}>
              {item.content}
            </Text>
          </View>
        )}
        ListEmptyComponent={
          <Text style={styles.empty}>
            Tell me where we're playing, {user.full_name.split(" ")[0]}.
          </Text>
        }
      />

      {error && <Text style={styles.error}>{error}</Text>}

      {/* Input row */}
      <KeyboardAvoidingView behavior={Platform.OS === "ios" ? "padding" : undefined} keyboardVerticalOffset={0}>
        <View style={styles.inputRow}>
          <TouchableOpacity style={styles.iconButton} onPress={handlePhoto} disabled={sending}>
            <Text style={styles.iconButtonText}>📷</Text>
          </TouchableOpacity>
          <TextInput
            style={styles.input}
            placeholder="Talk to your caddy…"
            placeholderTextColor={colors.muted}
            value={input}
            onChangeText={setInput}
            onSubmitEditing={handleSend}
            editable={!sending}
            multiline
          />
          <TouchableOpacity style={styles.sendButton} onPress={handleSend} disabled={sending}>
            {sending ? (
              <ActivityIndicator color={colors.cream} size="small" />
            ) : (
              <Text style={styles.sendButtonText}>↑</Text>
            )}
          </TouchableOpacity>
        </View>
      </KeyboardAvoidingView>
    </View>
  );
}

function formatHoleStatus(state: RoundState): string {
  const played = (state.hole_scores || [])
    .map((s, i) => ({ score: s, par: state.tee?.holes?.[i]?.par }))
    .filter((h): h is { score: number; par: number | undefined } => h.score !== null);
  const cur = state.current_hole || played.length + 1;
  if (played.length === 0) return `Hole ${cur}`;
  const total = played.reduce((a, h) => a + (h.score ?? 0), 0);
  const parTotal = played.reduce((a, h) => a + (h.par ?? 0), 0);
  if (!parTotal) return `Hole ${cur} · ${total}`;
  const vs = total - parTotal;
  return `Hole ${cur} · ${vs === 0 ? "E" : vs > 0 ? `+${vs}` : `${vs}`}`;
}

function extractGpsYards(events?: ChatEvent[]): number | null {
  if (!events) return null;
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === "gps_yardage") return e.yards_to_green;
  }
  return null;
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.cream },
  header: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    paddingTop: 58,
    paddingBottom: 10,
    paddingHorizontal: 16,
    backgroundColor: colors.forest,
  },
  headerTitle: { color: colors.cream, fontSize: 18, fontWeight: "800", letterSpacing: 3 },
  headerButtons: { flexDirection: "row", gap: 18 },
  headerAction: { color: colors.gold, fontSize: 14, fontWeight: "600" },
  roundBar: {
    backgroundColor: colors.forestDeep,
    paddingVertical: 6,
    paddingHorizontal: 16,
  },
  roundBarText: { color: colors.cream, fontSize: 13, fontWeight: "600" },
  weatherStrip: {
    backgroundColor: "#EAE4D6",
    paddingVertical: 5,
    paddingHorizontal: 16,
  },
  weatherText: { color: colors.ink, fontSize: 12 },
  list: { flex: 1 },
  listContent: { padding: 14, gap: 8 },
  bubble: {
    maxWidth: "84%",
    borderRadius: 16,
    paddingHorizontal: 14,
    paddingVertical: 10,
  },
  bubbleUser: { alignSelf: "flex-end", backgroundColor: colors.bubbleUser },
  bubbleAssistant: {
    alignSelf: "flex-start",
    backgroundColor: colors.bubbleAssistant,
    borderWidth: 1,
    borderColor: "#E4DFD3",
  },
  bubbleUserText: { color: colors.cream, fontSize: 15, lineHeight: 21 },
  bubbleAssistantText: { color: colors.ink, fontSize: 15, lineHeight: 21 },
  empty: { color: colors.muted, textAlign: "center", marginTop: 60, fontSize: 15 },
  error: { color: colors.error, textAlign: "center", paddingVertical: 4, fontSize: 13 },
  inputRow: {
    flexDirection: "row",
    alignItems: "flex-end",
    padding: 10,
    paddingBottom: 28,
    gap: 8,
    backgroundColor: colors.cream,
  },
  iconButton: {
    width: 42,
    height: 42,
    borderRadius: 21,
    backgroundColor: "#FFFFFF",
    borderWidth: 1,
    borderColor: "#E4DFD3",
    alignItems: "center",
    justifyContent: "center",
  },
  iconButtonText: { fontSize: 18 },
  input: {
    flex: 1,
    minHeight: 42,
    maxHeight: 110,
    backgroundColor: "#FFFFFF",
    borderRadius: 21,
    paddingHorizontal: 16,
    paddingTop: 11,
    paddingBottom: 11,
    fontSize: 15,
    color: colors.ink,
    borderWidth: 1,
    borderColor: "#E4DFD3",
  },
  sendButton: {
    width: 42,
    height: 42,
    borderRadius: 21,
    backgroundColor: colors.forest,
    alignItems: "center",
    justifyContent: "center",
  },
  sendButtonText: { color: colors.cream, fontSize: 20, fontWeight: "700" },
});
