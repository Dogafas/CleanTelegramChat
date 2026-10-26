# Local metric scoring gates candidates; Jev judges only candidates

Superseded in full by ADR 0009 and the evidence-based candidate routing cutover:
1. Aggregate Quality Score and composite weights are removed; candidate routing does not use a single numeric score.
2. Metrics serve as indirect review signals (posting frequency > 60 posts/month, low engagement ERR < 5%, suspiciously uniform reach CV < 0.15) rather than proof of botting or fraud.
3. Penalties for external links and decorative title characters are removed.
4. Jev evaluates every subscription that has usable post text without numeric metrics in state.
5. Candidates are prioritized into RED [Отписаться], YELLOW [Проверить] and GRAY [Недостаточно данных].

Historical context:
Channel quality was originally scored in two layers (local Quality Score 0–100 gated candidates below threshold 60, followed by Jev). This was replaced because numeric thresholds alone hid slop and clickbait while misclassifying valid channels, and aggregate scores presented misleading precision to the user.
