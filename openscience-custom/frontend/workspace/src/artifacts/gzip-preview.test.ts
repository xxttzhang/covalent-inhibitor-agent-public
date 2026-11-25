import { expect, test } from "bun:test"
import { gzipSync } from "node:zlib"
import { gzipPreview } from "./gzip-preview"

test("all gzip members including empty trailers are preserved", async () => {
  const bytes = new Uint8Array(Buffer.concat([gzipSync("first"), gzipSync("second"), gzipSync("")]))
  expect(new TextDecoder().decode(await gzipPreview(bytes))).toBe("firstsecond")
})

test("header-like bytes inside stored deflate data are not trusted", async () => {
  const original = new Uint8Array([1,31,139,8,0,0,0,0,0,0,3,2,3,4])
  expect(await gzipPreview(new Uint8Array(gzipSync(original, { level: 0 })))).toEqual(original)
})

test("corrupt, truncated and oversized data fail closed", async () => {
  const bytes = new Uint8Array(gzipSync("test"))
  await expect(gzipPreview(bytes, 2)).rejects.toThrow("limit")
  await expect(gzipPreview(bytes.slice(0, -2))).rejects.toThrow()
  bytes[bytes.length - 8] ^= 255
  await expect(gzipPreview(bytes)).rejects.toThrow()
})
