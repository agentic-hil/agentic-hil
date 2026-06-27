import fc from "fast-check";

export { fc };

export const safePathSegment = fc
  .array(fc.constantFrom(..."abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"), {
    minLength: 1,
    maxLength: 12,
  })
  .map((characters) => characters.join(""));
