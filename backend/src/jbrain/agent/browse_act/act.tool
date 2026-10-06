---
name: act
version: 1
permission: web
params:
  type: object
  properties:
    commands:
      type: array
      minItems: 1
      maxItems: 5
      description: 1 to 5 commands, run in order. The run stops early when the page moves to a new address, a target is gone, or a command is refused.
      items:
        type: object
        properties:
          do:
            type: string
            enum: [click, select, type, enter, goto, read, back, done]
          index:
            type: integer
            description: The element's number [n] on the latest page (click, select, type).
          value:
            type: string
            description: 'select: the option label. type: the text. goto: the http(s) address. read: optional phrase to start from. done: the answer.'
        required: [do]
  required: [commands]
---
Act on the page. Commands: click [n]; select [n] value=option; type [n] value=text (search,
filter, location and date fields only); enter (submit what you just typed); goto value=URL;
read (the page's full text, optionally from a phrase); back; done value=the answer — the
facts the goal asks for, copied exactly from the page, one per line, or "NOT FOUND: why".
done goes alone, as the only command, once the latest page shows the answer.
