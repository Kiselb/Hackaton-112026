# CLIP-017 customer gold v1 — ручная проверка

> Статус всех записей: `draft`. Метрики являются предварительными до независимого review.

| Case | Категория | Вопрос | Outcome | Источники | Положения | Решение reviewer |
|---|---|---|---|---|---|---|
| REAL-001 | single_document_fact | Кто назначает руководителя проекта в Дирекции ИБ? | answered | clip017:project-infosec | clip017:infosec-project:2.1-coordinator | ☐ approve ☐ reject |
| REAL-002 | single_document_fact | В какой срок Координатор департамента назначает РП по проекту Дирекции ИБ при наличии полной информации? | answered | clip017:project-infosec | clip017:infosec-project:2.7-one-day | ☐ approve ☐ reject |
| REAL-003 | single_document_fact | Что происходит с карточкой проекта Дирекции ИБ, если необходимой информации или ответа от SS нет более пяти рабочих дней? | answered | clip017:project-infosec | clip017:infosec-project:2.3-rejected | ☐ approve ☐ reject |
| REAL-004 | single_document_fact | Какие материалы в первой строке таблицы карточки проекта Дирекции ИБ обязательны, а какие прикладываются при наличии? | answered | clip017:project-infosec | clip017:infosec-project:1.2-required-docs | ☐ approve ☐ reject |
| REAL-005 | single_document_fact | Кому запрещено предоставлять доступ к сайту проекта Дирекции ИБ? | answered | clip017:project-infosec | clip017:infosec-project:4.3-site-access | ☐ approve ☐ reject |
| REAL-006 | single_document_fact | Могут ли SS и TSS выдать заказчику официальное ТКП без согласования с ПА? | answered | clip017:presales-infosec | clip017:infosec-presales:2.3-tkp | ☐ approve ☐ reject |
| REAL-007 | single_document_fact | Когда заявка на пресейл Дирекции ИБ дисквалифицируется из-за непредоставленных исходных данных? | answered | clip017:presales-infosec | clip017:infosec-presales:3.4-disqualification | ☐ approve ☐ reject |
| REAL-008 | single_document_fact | За сколько времени ПА должен принять назначенную заявку в работу в процессе пресейла Дирекции ИБ? | answered | clip017:presales-infosec | clip017:infosec-presales:5.1-accept | ☐ approve ☐ reject |
| REAL-009 | single_document_fact | Какие роли обязательно должны быть у субподрядчика по экзотической теме в пресейле Дирекции ИБ? | answered | clip017:presales-infosec | clip017:infosec-presales:4.2.3-exotic | ☐ approve ☐ reject |
| REAL-010 | single_document_fact | Не позднее какого срока должна быть инициирована заявка на пресейл ДБРЦТ до подачи предложения заказчику? | answered | clip017:project-dbrct | clip017:dbrct:1.5-initiation-deadline | ☐ approve ☐ reject |
| REAL-011 | single_document_fact | Можно ли использовать бюджетную оценку ДБРЦТ для конкурса, контрактования или определения начальной максимальной цены? | answered | clip017:project-dbrct | clip017:dbrct:2.4-budget-limit | ☐ approve ☐ reject |
| REAL-012 | single_document_fact | Кто назначает РП на пресейл ДБРЦТ при достаточности данных? | answered | clip017:project-dbrct | clip017:dbrct:2.1.2-presales-appointments | ☐ approve ☐ reject |
| REAL-013 | single_document_fact | За сколько времени до подачи конкурса должен быть инициирован расчёт в Технической дирекции? | answered | clip017:project-tech | clip017:tech:1.5-two-weeks | ☐ approve ☐ reject |
| REAL-014 | single_document_fact | Что обязан сделать TPS, если нет собственных ресурсов, нельзя привлечь подрядчика или отсутствуют необходимые компетенции? | answered | clip017:project-tech | clip017:tech:2.8-refusal | ☐ approve ☐ reject |
| REAL-015 | multi_document | Кто назначает РП на этапе пресейла в ДБРЦТ и в Технической дирекции? | answered | clip017:project-dbrct<br>clip017:project-tech | clip017:dbrct:2.1.2-presales-appointments<br>clip017:tech:4.1-presales-rp | ☐ approve ☐ reject |
| REAL-016 | multi_document | Сравните последствия отсутствия исходных данных при назначении РП проекта Дирекции ИБ и при обработке пресейловой заявки Дирекции ИБ. | answered | clip017:project-infosec<br>clip017:presales-infosec | clip017:infosec-project:2.3-rejected<br>clip017:infosec-presales:3.4-disqualification | ☐ approve ☐ reject |
| REAL-017 | multi_document | Можно ли использовать бюджетную оценку для конкурса и контрактования в ДБРЦТ и в Технической дирекции? | answered | clip017:project-dbrct<br>clip017:project-tech | clip017:dbrct:2.4-budget-limit<br>clip017:tech:2.4-budget-limit | ☐ approve ☐ reject |
| REAL-018 | multi_document | Кто отвечает за подбор или выбор подрядчика на пресейле ДБРЦТ и Технической дирекции? | answered | clip017:project-dbrct<br>clip017:project-tech | clip017:dbrct:4.1-contractor-search<br>clip017:tech:2.11-contractor-owner | ☐ approve ☐ reject |
| REAL-019 | multi_document | Каков срок назначения РП при полной информации в Дирекции ИБ и на этапе реализации Технической дирекции? | answered | clip017:project-infosec<br>clip017:project-tech | clip017:infosec-project:2.7-one-day<br>clip017:tech:9.1-implementation-deadline | ☐ approve ☐ reject |
| REAL-020 | multi_document | Что предусматривают три проектных регламента, если внутренних ресурсов для выполнения работ не хватает? | answered | clip017:project-dbrct<br>clip017:project-infosec<br>clip017:project-tech | clip017:dbrct:3.1-no-resources<br>clip017:infosec-project:7.3.4-no-resources<br>clip017:tech:2.8-refusal | ☐ approve ☐ reject |
| REAL-021 | multi_document | Кого назначают ответственным при достаточности данных в пресейле ДБРЦТ, Технической дирекции и Дирекции ИБ? | answered | clip017:project-dbrct<br>clip017:project-tech<br>clip017:presales-infosec | clip017:dbrct:2.1.2-presales-appointments<br>clip017:tech:3.1-architect-owner<br>clip017:infosec-presales:4.1-assign-pa | ☐ approve ☐ reject |
| REAL-022 | ambiguous_clarification | Кто назначает РП? | answered | clip017:project-tech | clip017:tech:9.1-implementation-deadline<br>clip017:tech:9.3-implementation-owner | ☐ approve ☐ reject |
| REAL-023 | ambiguous_clarification | Что происходит при недостатке данных? | answered | clip017:project-infosec | clip017:infosec-project:2.2-incomplete<br>clip017:infosec-project:2.3-rejected | ☐ approve ☐ reject |
| REAL-024 | ambiguous_clarification | Кто выбирает подрядчика? | answered | clip017:project-tech | clip017:tech:2.11-contractor-owner | ☐ approve ☐ reject |
| REAL-025 | ambiguous_clarification | В какой срок надо подать заявку? | answered | clip017:project-dbrct | clip017:dbrct:1.5-initiation-deadline | ☐ approve ☐ reject |
| REAL-026 | ambiguous_clarification | Можно ли использовать бюджетную оценку? | answered | clip017:project-dbrct | clip017:dbrct:2.4-budget-limit | ☐ approve ☐ reject |
| REAL-027 | ambiguous_clarification | Когда назначается архитектор? | answered | clip017:project-tech | clip017:tech:3.1-architect-owner<br>clip017:tech:3.2-architect-data | ☐ approve ☐ reject |
| REAL-028 | insufficient_data | Каков оклад руководителя проекта в Дирекции ИБ? | quality_review_required | — | — | ☐ approve ☐ reject |
| REAL-029 | insufficient_data | Кто утверждает ежегодный отпуск сотрудников Технической дирекции? | quality_review_required | — | — | ☐ approve ☐ reject |
| REAL-030 | insufficient_data | Каков точный SLA в рабочих днях на поиск и согласование подрядчика в ДБРЦТ? | quality_review_required | — | — | ☐ approve ☐ reject |
| REAL-031 | insufficient_data | Каков максимальный денежный бюджет проекта Дирекции ИБ? | quality_review_required | — | — | ☐ approve ☐ reject |
| REAL-032 | insufficient_data | Сколько лет должны храниться карточки закрытых проектов? | quality_review_required | — | — | ☐ approve ☐ reject |
| REAL-033 | insufficient_data | Какой из четырёх регламентов автоматически отменяет остальные как более новый? | quality_review_required | — | — | ☐ approve ☐ reject |
| REAL-034 | scope_and_constraints | Когда в проекте Дирекции ИБ можно назначить администратора проекта вместо руководителя проекта? | answered | clip017:project-infosec | clip017:infosec-project:2.6-admin | ☐ approve ☐ reject |
| REAL-035 | scope_and_constraints | В каком случае можно изменить уже утверждённую оценку трудозатрат Технической дирекции? | answered | clip017:project-tech | clip017:tech:2.16-approved-estimate | ☐ approve ☐ reject |
| REAL-036 | scope_and_constraints | Что должен сделать SS в Дирекции ИБ, если один запрос одновременно содержит внедрение и техническую поддержку? | answered | clip017:presales-infosec | clip017:infosec-presales:2.5-multiple-topics | ☐ approve ☐ reject |
