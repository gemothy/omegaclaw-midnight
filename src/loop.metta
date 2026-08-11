(= (maxNewInputLoops) (empty))
(= (maxWakeLoops) (empty))
(= (sleepInterval) (empty))
(= (provider) (empty))
(= (maxOutputToken) (empty))
(= (reasoningMode) (empty))
(= (wakeupInterval) (empty))
(= (memoryDirectory) (empty))
(= (spamShield) (empty)) ; TODO: this parameter is considered deprecated

(= (initLoop)
   ; CADENCE IS TUNED FOR A LIVE WORLD, not for a chat assistant.
   ;
   ; &loops is a budget of decision cycles. The loop spends it, then goes dormant
   ; and refills only when wakeupInterval has passed. Upstream that is 1 extra
   ; loop every 600 seconds, which is right for an agent that waits on a human
   ; and wrong for Midnight City: a thread there dies after about sixty seconds,
   ; so a ten minute nap means every person who writes is answered far too late
   ; or not at all. Measured on this fork before the change: the agent made its
   ; last decision at 06:18:48 and was still asleep at 06:25:18, iterating once a
   ; second and doing nothing - absent for well over ninety percent of the time,
   ; which no amount of reachability work could compensate for.
   ;
   ; It also invalidated my own throughput readings: bursts measured right after
   ; a restart were spending the startup budget of 50, not a steady state.
   ;
   ; 5 loops every 15 seconds is near-continuous while still bounded, so a
   ; runaway cannot spin the GPU flat out forever. Each decision costs about five
   ; seconds of a short generation, and the box stays well inside the memory cap.
   (progn (configure maxNewInputLoops 50) ;20
          (configure maxWakeLoops 5)
          (configure spamShield False)
          (configure sleepInterval 1) ;10
          (configure provider Anthropic)
          (configure maxOutputToken 6000)
          (configure reasoningMode medium)
          (configure wakeupInterval 15) ;upstream 600=10 minutes; see the note above
          (configure memoryDirectory ./)
          (change-state! &prevmsg "")
          (change-state! &lastresults "")
          (change-state! &loops (maxNewInputLoops))
          ))

(= (initKnowledge)
   (progn (log INFO "loop" "Initializing knowledge base")
          (if (== (embeddingprovider) OpenAI)
               (log INFO "loop" (py-call (rag.init_knowledge "OpenAI")))
               (log INFO "loop" (py-call (rag.init_knowledge "Local"))))))

(= (getContext)
   (string-safe (py-str ("PROMPT: " (getPrompt (provider)) " SKILLS: " (getSkills)
                         (newline) (getPromptExtensions) (newline)
                         " OUTPUT_FORMAT: Up to 5 lines, do not wrap quotes around args, do not use variables:" (newline)
                         " toolName1 arg1" (newline)
                         " toolName2 arg2" (newline)
                         " toolName3 arg3" (newline)
                         " toolName4 arg4" (newline)
                         " toolName5 arg5" (newline)
                         " SAVE_PERMANENT_FILES_DIR: " (memoryDirectory)
                         " LAST_SKILL_USE_RESULTS: you must rectify ALERT_FAILED skill use cases " (last_chars (get-state &lastresults) (maxFeedback)) 
                         " HISTORY: " (getHistory) 
                         " TIME: " (get_time_as_string)))))

(= (getPromptExtensions)
   (join (newline) (collapse (prompt-extension $_))))

; dynamic prompt extension placeholder to eliminate error when no extension is added
(= (prompt-extension placeholder) (empty))

(= (HandleError $msg $cmd $sexpr)
   (case $sexpr (((Error $a $b) (let $new (append (get-state &error) (($msg $cmd)))
                                          (progn (change-state! &error $new) (ALERT_FAILED $a $b))))
                 ($else $sexpr))))

(= (omegaclaw)
   (omegaclaw 1))
(= (omegaclaw $k)
   (progn (if (== $k 1) (progn (initConfig)
                               (initLoop)
                               (initLogger)
                               (applySecurityPolicy)
                               (initMemory)
                               (initKnowledge)
                               (initPlugins)
                               (initChannels)
                               (commChannelSend (version))
                               (llmProviderStart (provider)))
                        (change-state! &loops (- (get-state &loops) 1)))
          (let $prompt (getContext)
               (progn (log INFO "loop" (---------iteration $k))
                      (heartbeat $k)
                      (let* (($msgrcv (string-safe (repr (receive))))
                             ($msgnew (prog1 (and (> (string_length $msgrcv) 0) (!= $msgrcv (get-state &prevmsg)))
                                             (if (> (string_length $msgrcv) 0) (change-state! &prevmsg $msgrcv) _)))
                             ($msg (get-state &prevmsg))
                             ($_ (if (and (> $k 1) $msgnew)
                                     (change-state! &loops (maxNewInputLoops)) _)))
                            (if (> (get-state &loops) 0)
                                (let* (($lastmessage (if $msgnew (HUMAN-MSG: $msg) (if (spamShield) " DO NOT RE-SEND OR SPAM!" "")))
                                       ($_ (change-state! &nextWakeAt (+ (get_time) (wakeupInterval))))
                                       ($_ (log INFO "loop" $lastmessage))
                                       ($send (py-str ($prompt :-:-:-: $lastmessage)))
                                       ($_ (log INFO "loop" (CHARS_SENT: (string_length $send) $send)))
                                       ($respi (llmProviderChat $send (maxOutputToken) (reasoningMode)))
                                       ($resp (py-call (helper.balance_parentheses $respi)))
                                       ($response (if (== "(" (first_char $resp)) $resp (progn (log INFO "loop" $resp) (repr (REMEMBER:OUTPUT_NOTHING_ELSE_THAN: ((skill arg) ...))))))
                                       ($sexpr (catch (sread $response)))
                                       ($_ (change-state! &error ()))
                                       ($mcerr (HandleError MULTI_COMMAND_FAILURE_NOTHING_WAS_DONE_PLEASE_CORRECT_PARENTHESES_AND_USE_QUOTES_AND_RETRY $response $sexpr))
                                       ($_ (log INFO "loop" (RESPONSE: $sexpr)))
                                       ($results (if (== $mcerr $sexpr)
                                                      (RESULTS: (collapse (let $s (superpose $sexpr) (COMMAND_RETURN: ($s (HandleError SINGLE_COMMAND_ERROR_NOTHING_WAS_DONE_PLEASE_FIX_AND_RETRY $s (catch (let $R (eval $s) (py-call (helper.normalize_string $R))))))))))
                                                      (RESULTS: $mcerr)))
                                       ($_ (log INFO "loop" (RESPONSE: $results))))
                                      (progn (if (or $msgnew  (not (== $sexpr ()))) (addToHistory $msg $response $sexpr $msgnew) _)
                                             (change-state! &lastresults (string-safe (repr $results)))))
                                (if (> (get_time) (get-state &nextWakeAt))
                                    (change-state! &loops (+ 1 (maxWakeLoops))) _)))
                      (sleep (sleepInterval))
                      (cut)
                      (gc)
                      (omegaclaw (+ 1 $k))))))
