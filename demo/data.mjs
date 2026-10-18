// Deterministic, invented measurements. Never import production recordings here.
export const channels = [
  { id: 1, name: 'Pump drive end', direction: 'Horizontal · Bearing A', level: 2.35, acceleration: 0.28 },
  { id: 2, name: 'Pump non-drive end', direction: 'Vertical · Bearing B', level: 1.82, acceleration: 0.22 },
  { id: 3, name: 'Motor drive end', direction: 'Horizontal · Bearing C', level: 1.45, acceleration: 0.17 },
  { id: 4, name: 'Motor non-drive end', direction: 'Axial · Bearing D', level: 1.12, acceleration: 0.14 },
];
export const scenarios = {
  normal: 'Normal operation: all four sample channels report below the illustrative warning level.',
  rising: 'Rising vibration: CH1 increases above the example warning level. Compare its trend and spectrum.',
  missing: 'Missing sensor: CH3 has stopped reporting. Its last value stays visible and is marked stale.',
};
export function sample(channel, minute, scenario = 'normal') {
  const ripple = Math.sin(minute * 0.32 + channel.id) * 0.09 + Math.cos(minute * 0.91 + channel.id) * 0.035;
  const rise = scenario === 'rising' && channel.id === 1 ? Math.min(3.0, Math.max(0, minute - 20) * 0.075) : 0;
  return +(channel.level + ripple + rise).toFixed(3);
}
export function snapshot(scenario, tick) {
  if (!(Object.hasOwn(scenarios, scenario)) || !Number.isInteger(tick) || tick < 0) throw new Error('Invalid demo state');
  const minute = 59 + tick;
  return channels.map(channel => {
    const stale = scenario === 'missing' && channel.id === 3;
    const captureMinute = stale ? 54 : minute;
    const value = sample(channel, captureMinute, scenario);
    return { ...channel, stale, value, captureMinute, ageMinutes: minute - captureMinute, warning: !stale && value >= 4.5,
      history: Array.from({ length: 60 }, (_, i) => {
        const time = minute - 59 + i;
        return { minute: time, value: stale && time > captureMinute ? null : sample(channel, time, scenario) };
      }) };
  });
}
export function spectrum(channel, scenario, baseline = false) {
  const factor = scenario === 'rising' && channel.id === 1 && !baseline ? 2.25 : 1;
  return Array.from({ length: 501 }, (_, hz) => {
    let amplitude = 0.003 + Math.abs(Math.sin(hz * 1.71 + channel.id)) * 0.003;
    for (let harmonic = 1; harmonic <= 5; harmonic++) {
      const distance = (hz - harmonic * 1480 / 60) / 1.25;
      amplitude += Math.exp(-distance * distance / 2) * (0.12 / harmonic) * factor * (channel.level / 2.35);
    }
    return { hz, value: amplitude };
  });
}
export function replayTime(minute) {
  const seconds = ((12 * 3600 + minute * 60) % 86400 + 86400) % 86400;
  return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60].map(x => String(x).padStart(2, '0')).join(':');
}
export function events(scenario, tick) {
  const minute = 59 + tick;
  const rows = [{ minute: 0, channel: 'All channels', state: 'Info', message: 'Sample replay started with four synthetic measurement points.' }];
  if (scenario === 'rising') rows.unshift({ minute: 50, channel: 'CH1 · Pump drive end', state: 'Warning', message: 'Example velocity crossed 4.5 mm/s RMS during the sample ramp.' });
  if (scenario === 'missing') rows.unshift({ minute: 57, channel: 'CH3 · Motor drive end', state: 'Stale', message: `No sample for ${minute - 54} replay minutes; exceeds three example 60-second publishing intervals.` });
  return rows;
}
export function csv(channel) {
  const lines = ['data_source,replay_time_utc,channel,velocity_mm_s_rms,filter,detection'];
  for (const point of channel.history) lines.push(`synthetic_demo,${replayTime(point.minute)},CH${channel.id},${point.value ?? ''},10Hz-1kHz,RMS`);
  return lines.join('\r\n') + '\r\n';
}
