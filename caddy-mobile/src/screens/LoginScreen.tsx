import { useState } from "react";
import {
  ActivityIndicator, KeyboardAvoidingView, Platform, StyleSheet,
  Text, TextInput, TouchableOpacity, View,
} from "react-native";
import { api, User } from "../api";
import { setToken } from "../auth";
import { colors } from "../theme";

export default function LoginScreen({ onLogin }: { onLogin: (user: User) => void }) {
  const [username, setUsername] = useState("");
  const [pin, setPin] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleLogin() {
    if (!username.trim() || !pin.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const { user, token } = await api.login(username.trim(), pin.trim());
      await setToken(token);
      onLogin(user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      <Text style={styles.logo}>CADDY</Text>
      <Text style={styles.tagline}>The AI caddy that knows your game</Text>

      <TextInput
        style={styles.input}
        placeholder="Username"
        placeholderTextColor={colors.muted}
        autoCapitalize="none"
        autoCorrect={false}
        value={username}
        onChangeText={setUsername}
      />
      <TextInput
        style={styles.input}
        placeholder="PIN"
        placeholderTextColor={colors.muted}
        keyboardType="number-pad"
        secureTextEntry
        value={pin}
        onChangeText={setPin}
        onSubmitEditing={handleLogin}
      />

      {error && <Text style={styles.error}>{error}</Text>}

      <TouchableOpacity style={styles.button} onPress={handleLogin} disabled={busy}>
        {busy ? (
          <ActivityIndicator color={colors.cream} />
        ) : (
          <Text style={styles.buttonText}>Sign in</Text>
        )}
      </TouchableOpacity>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.cream,
    justifyContent: "center",
    paddingHorizontal: 32,
  },
  logo: {
    fontSize: 44,
    fontWeight: "800",
    color: colors.forest,
    textAlign: "center",
    letterSpacing: 6,
  },
  tagline: {
    color: colors.muted,
    textAlign: "center",
    marginBottom: 40,
    marginTop: 6,
  },
  input: {
    backgroundColor: "#FFFFFF",
    borderRadius: 12,
    paddingHorizontal: 16,
    paddingVertical: 14,
    fontSize: 16,
    color: colors.ink,
    marginBottom: 12,
    borderWidth: 1,
    borderColor: "#E4DFD3",
  },
  button: {
    backgroundColor: colors.forest,
    borderRadius: 12,
    paddingVertical: 15,
    alignItems: "center",
    marginTop: 8,
  },
  buttonText: { color: colors.cream, fontSize: 16, fontWeight: "700" },
  error: { color: colors.error, textAlign: "center", marginBottom: 8 },
});
