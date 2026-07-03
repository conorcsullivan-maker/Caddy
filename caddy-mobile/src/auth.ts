// Session-token storage. SecureStore keeps the token in the iOS keychain
// (falls back to localStorage on web dev builds, where SecureStore is
// unavailable).
import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";

const KEY = "caddy_session_token";

export async function getToken(): Promise<string | null> {
  if (Platform.OS === "web") return globalThis.localStorage?.getItem(KEY) ?? null;
  return SecureStore.getItemAsync(KEY);
}

export async function setToken(token: string): Promise<void> {
  if (Platform.OS === "web") {
    globalThis.localStorage?.setItem(KEY, token);
    return;
  }
  await SecureStore.setItemAsync(KEY, token);
}

export async function clearToken(): Promise<void> {
  if (Platform.OS === "web") {
    globalThis.localStorage?.removeItem(KEY);
    return;
  }
  await SecureStore.deleteItemAsync(KEY);
}
