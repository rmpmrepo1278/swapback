# Third-party notices

swapback bundles rule data from the following projects. Their licenses are reproduced in full.

## gitleaks v8.30.1

- What: the default detection rules, config/gitleaks.toml at tag v8.30.1, vendored as swapback/vendor/gitleaks/gitleaks.toml (unmodified).
- Source: https://github.com/gitleaks/gitleaks
- License: MIT

```
MIT License

Copyright (c) 2019 Zachary Rice

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Microsoft Presidio (presidio-analyzer 2.2.366)

- What: the regex patterns, scores and context words of the predefined recognizers EmailRecognizer, CreditCardRecognizer, IbanRecognizer, UsSsnRecognizer, IpRecognizer and UrlRecognizer, vendored as swapback/vendor/presidio/patterns.json; their validation logic is ported in swapback/detect.py.
- Source: https://github.com/microsoft/presidio
- License: MIT

```
MIT License

Copyright (c) Microsoft Corporation.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## SCOWL (Spell Checker Oriented Word Lists), size 50

- What: the lower-case, letters-only words of the American English list at SCOWL size 50 (the list Debian ships as
  `wamerican`), one per line, gzipped, as swapback/data/words.txt.gz. Used only to tell whether a listed name is made of
  ordinary words.
- Source: http://wordlist.aspell.net/ (Kevin Atkinson)
- License: the SCOWL copyright and permission notice (MIT-like), reproduced below.

```
The collective work is Copyright 2000-2011 by Kevin Atkinson as well
as any of the copyrights mentioned below:

  Copyright 2000-2011 by Kevin Atkinson

  Permission to use, copy, modify, distribute and sell these word
  lists, the associated scripts, the output created from the scripts,
  and its documentation for any purpose is hereby granted without fee,
  provided that the above copyright notice appears in all copies and
  that both that copyright notice and this permission notice appear in
  supporting documentation. Kevin Atkinson makes no representations
  about the suitability of this array for any purpose. It is provided
  "as is" without express or implied warranty.
```

SCOWL also incorporates word lists by Alan Beale, the Moby project (public domain), Brian Kelk's UK English Wordlist
and others; their notices are in the SCOWL README (https://github.com/en-wl/wordlist/blob/master/scowl/README).
