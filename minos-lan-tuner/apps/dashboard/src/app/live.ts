import { useQuery } from "@tanstack/react-query";
import { api, liveInterval } from "./api";

export function useLive<T>(key: unknown[], path: string) {
  return useQuery({
    queryKey: key,
    queryFn: () => api<T>(path),
    refetchInterval: liveInterval,
  });
}
