"""
Tests für _parse_recorded_filename() (recorder.py).

Der alte naive "_"-Split zwischen Kategorie und Titel (cat_title_parts =
rest_after_channel.split("_", 1)) hat mehrteilige Kategorienamen falsch
zugeordnet, sobald die Kategorie selbst einen "_" enthielt (z.B. durch
Leerzeichen-Ersetzung) - der erste Wortteil landete als "Kategorie", der Rest
fälschlich im Titel. Die Kategorie wird jetzt in eckigen Klammern kodiert
(siehe out_pattern in record_loop()), was die Trennung eindeutig macht.
"""


class TestParseRecordedFilename:
    def test_multi_word_category_with_underscore_is_not_split(self, recorder):
        full_stem = "2026-09-17_14-30_somechannel_[Just_Chatting]_Some_Cool_Title"

        date_str, time_str, channel, category, title = recorder._parse_recorded_filename(
            full_stem, "somechannel"
        )

        assert date_str == "2026-09-17"
        assert time_str == "14-30"
        assert channel == "somechannel"
        assert category == "Just_Chatting"
        assert title == "Some_Cool_Title"

    def test_category_without_underscore(self, recorder):
        full_stem = "2026-09-17_14-30_somechannel_[Minecraft]_Building_a_house"

        _, _, _, category, title = recorder._parse_recorded_filename(full_stem, "somechannel")

        assert category == "Minecraft"
        assert title == "Building_a_house"

    def test_channel_name_itself_containing_underscore(self, recorder):
        full_stem = "2026-09-17_14-30_some_channel_[Just_Chatting]_Title_here"

        _, _, channel, category, title = recorder._parse_recorded_filename(
            full_stem, "some_channel"
        )

        assert channel == "some_channel"
        assert category == "Just_Chatting"
        assert title == "Title_here"

    def test_missing_category_brackets_falls_back_to_naive_split(self, recorder):
        """Legacy-Format (vor diesem Fix) ohne eckige Klammern - best effort wie zuvor."""
        full_stem = "2026-09-17_14-30_somechannel_Minecraft_Building_a_house"

        _, _, _, category, title = recorder._parse_recorded_filename(full_stem, "somechannel")

        assert category == "Minecraft"
        assert title == "Building_a_house"

    def test_empty_category_and_title_fall_back_to_defaults(self, recorder):
        full_stem = "2026-09-17_14-30_somechannel_[]_"

        _, _, _, category, title = recorder._parse_recorded_filename(full_stem, "somechannel")

        assert category == "NoCategory"
        assert title == "Untitled"

    def test_unexpected_format_missing_channel_prefix(self, recorder):
        """Streamlink hat den Namen unerwartet geschrieben - best effort, kein Crash."""
        full_stem = "2026-09-17_14-30_totally-different-format"

        date_str, time_str, channel, category, title = recorder._parse_recorded_filename(
            full_stem, "somechannel"
        )

        assert date_str == "2026-09-17"
        assert time_str == "14-30"
        assert channel == "somechannel"
        assert category  # kein Crash, irgendein Fallback-Wert
        assert title
