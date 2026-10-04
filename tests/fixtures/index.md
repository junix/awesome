# Offline index fixture

A prose [discovery link](https://example.org/not-indexed) is not an entry.

| 名称 | 内容 |
| --- | --- |
| [First](HTTPS://Example.COM:443/list#intro) | [Unrelated description link](https://example.org/description) |
| [Duplicate](https://example.com/list#other) | Same entry URL. |
| [Distinct query](https://example.com/list?view=all) | Query is significant. |
| [Distinct path case](https://example.com/List) | Path case is significant. |
| [GitHub topic](https://github.com/topics/awesome) | Not assumed to be a repository. |

```markdown
| [Example only](https://example.org/code) | Fenced code isn't indexed. |
```

~~~text
| [Another example](https://example.org/code2) | Also not indexed. |
~~~
