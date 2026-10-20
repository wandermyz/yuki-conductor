/** Render a search snippet, turning its \x02…\x03 markers into <mark>. */
export default function Snippet({ text }: { text: string }) {
  // "a\x02b\x03c" splits on \x02 into ["a", "b\x03c"]: every piece after the
  // first opens with a matched term, ending at its \x03.
  const [head, ...rest] = text.split("\x02");
  return (
    <>
      {head}
      {rest.map((p, i) => {
        const [term, tail] = p.split("\x03");
        return (
          <span key={i}>
            <mark>{term}</mark>
            {tail}
          </span>
        );
      })}
    </>
  );
}
