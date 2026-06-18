set -e

python3 scripts/download_sciebo_webdav.py \
    --share-url "https://rwth-aachen.sciebo.de/s/gEMDMstBiSHAAAS" \
    --output "large_models.zip"
unzip large_models.zip -d large_models

echo "Moving large benchmark files"
for benchmark_dir in large_models/vnncomp2026/*
do
    [ -d "$benchmark_dir" ] || continue

    benchmark=$(basename "$benchmark_dir")
    seed_dir=""
    for candidate in "$benchmark_dir"/seed_*
    do
        if [ -d "$candidate" ]; then
            seed_dir="$candidate"
            break
        fi
    done

    if [ -n "$seed_dir" ]; then
        echo "Moving $benchmark from $(basename "$seed_dir")"
        find "$seed_dir" -type f | while read -r source_file
        do
            relative_path=${source_file#"$seed_dir"/}
            target_file="benchmarks/$benchmark/$relative_path"
            mkdir -p "$(dirname "$target_file")"
            mv "$source_file" "$target_file"
        done
        continue
    fi

    for version_dir in "$benchmark_dir"/*
    do
        [ -d "$version_dir" ] || continue
        version=$(basename "$version_dir")

        case "$version" in
            [0-9]*.[0-9]*) ;;
            *) continue ;;
        esac

        echo "Moving $benchmark $version"
        find "$version_dir" -type f | while read -r source_file
        do
            relative_path=${source_file#"$version_dir"/}
            target_file="benchmarks/$benchmark/$version/$relative_path"
            mkdir -p "$(dirname "$target_file")"
            mv "$source_file" "$target_file"
        done

        vnnlib_dir="$benchmark_dir/vnnlib$version"
        if [ -d "$vnnlib_dir" ]; then
            find "$vnnlib_dir" -type f | while read -r source_file
            do
                relative_path=${source_file#"$vnnlib_dir"/}
                target_file="benchmarks/$benchmark/$version/vnnlib/$relative_path"
                mkdir -p "$(dirname "$target_file")"
                mv "$source_file" "$target_file"
            done
        fi
    done
done
rm -r large_models large_models.zip

echo "Unzipping"
gunzip -r benchmarks/
