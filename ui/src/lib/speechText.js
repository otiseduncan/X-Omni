/**
 * Convert assistant display text into speech-friendly plain text.
 *
 * Chat may legitimately contain Markdown, links, code fences, and emphasis.
 * Browser SpeechSynthesis does not understand Markdown; some voices literally
 * pronounce `**` as "asterisk asterisk".  TTS therefore gets a separate plain-
 * speech representation while the visible chat keeps its normal formatting.
 */
export function toSpeechText(value) {
  let text = String(value || "");
  if (!text.trim()) return "";

  // Markdown images/links: speak the human label, never the URL syntax.
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");
  text = text.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");

  // Fenced and inline code delimiters are formatting, not spoken words.
  text = text.replace(/```[^\n]*\n?/g, "");
  text = text.replace(/```/g, "");
  text = text.replace(/`([^`]*)`/g, "$1");

  // Remove heading/list/quote formatting at line starts.
  text = text.replace(/^\s{0,3}#{1,6}\s+/gm, "");
  text = text.replace(/^\s*>+\s?/gm, "");
  text = text.replace(/^\s*[-+*]\s+/gm, "");

  // Emphasis/strike markers. Remove the markers but preserve their words.
  text = text.replace(/\*\*\*|___/g, "");
  text = text.replace(/\*\*|__/g, "");
  text = text.replace(/~~/g, "");
  text = text.replace(/(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)/g, "");

  // Raw URLs are useful visually but awful when spoken character by character.
  text = text.replace(/https?:\/\/\S+/gi, "");

  // Strip basic HTML that may arrive in generated/tool text.
  text = text.replace(/<[^>]+>/g, " ");

  // Backslash Markdown escapes should become the escaped character.
  text = text.replace(/\\([\\`*_{}\[\]()#+\-.!>])/g, "$1");

  // Final hard stop for formatting asterisks so no voice can pronounce them.
  text = text.replace(/\*/g, "");

  return text
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/[ \t]{2,}/g, " ")
    .trim();
}
